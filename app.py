import os
import time
import json
import logging
import threading  # <--- IMPORTANTE: Para rodar tarefas em background
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# --- CONFIGURAÇÃO INICIAL ---
app = Flask(__name__)

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- BANCO DE DADOS ---
home_dir = os.environ.get('HOME', os.path.abspath(os.path.dirname(__file__)))
db_path = os.path.join(home_dir, 'assinaturas.db')

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# --- VARIÁVEIS GLOBAIS DE CONTROLE ---
ESTADO_GRAVACAO = {
    "status": "OCIOSO", 
    "mensagem": "Nenhuma gravação em andamento",
    "buffer": [],
    "ultima_leitura": 0.0,
    "aparelho_alvo": ""
}

# --- MODELOS DO BANCO ---
class LeituraTempoReal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    corrente = db.Column(db.Float, nullable=False)
    watts = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)

class AssinaturaAparelho(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_aparelho = db.Column(db.String(100), nullable=False)
    dados_json = db.Column(db.Text, nullable=False) 
    data_criacao = db.Column(db.DateTime, default=datetime.now)

# Inicializa Banco
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        logger.error(f"Erro DB: {e}")

# --- FUNÇÃO WORKER (THREAD) ---
def worker_gravacao(app_context, nome_aparelho):
    global ESTADO_GRAVACAO
    with app_context:
        logger.info(f"[THREAD] Iniciando monitoramento para: {nome_aparelho}")
        start_wait = time.time()
        
        # FASE 1: Esperar Gatilho
        ESTADO_GRAVACAO['status'] = "AGUARDANDO_GATILHO"
        ESTADO_GRAVACAO['buffer'] = []
        
        while True:
            if (time.time() - start_wait) > 120:
                ESTADO_GRAVACAO['status'] = "ERRO"
                ESTADO_GRAVACAO['mensagem'] = "Timeout: Aparelho não ligou."
                return
            
            if ESTADO_GRAVACAO['ultima_leitura'] > 30.0:
                break
            time.sleep(0.2)

        # FASE 2: Gravação
        logger.info("[THREAD] Gatilho acionado! Gravando...")
        ESTADO_GRAVACAO['status'] = "GRAVANDO"
        start_collect = time.time()

        while len(ESTADO_GRAVACAO['buffer']) < 10:
            if (time.time() - start_collect) > 60:
                ESTADO_GRAVACAO['status'] = "ERRO"
                ESTADO_GRAVACAO['mensagem'] = "Timeout coleta."
                return
            time.sleep(0.1)

        # FASE 3: Salvar
        try:
            valores_finais = list(ESTADO_GRAVACAO['buffer'])
            nova_assinatura = AssinaturaAparelho(
                nome_aparelho=nome_aparelho,
                dados_json=json.dumps(valores_finais)
            )
            db.session.add(nova_assinatura)
            db.session.commit()
            
            ESTADO_GRAVACAO['status'] = "CONCLUIDO"
            ESTADO_GRAVACAO['mensagem'] = f"Sucesso! {len(valores_finais)} pontos."
            logger.info("[THREAD] Gravação concluída com sucesso.")
            
        except Exception as e:
            ESTADO_GRAVACAO['status'] = "ERRO"
            ESTADO_GRAVACAO['mensagem'] = f"Erro ao salvar: {str(e)}"

# --- ROTAS ---

@app.route('/')
def home():
    return "API Async - Status: ONLINE", 200

# 1. RECEBE DADOS
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ESTADO_GRAVACAO
    try:
        data = request.get_json()
        watts = float(data.get('watts', 0.0))
        ESTADO_GRAVACAO['ultima_leitura'] = watts

        if ESTADO_GRAVACAO['status'] == "GRAVANDO":
            if len(ESTADO_GRAVACAO['buffer']) < 10:
                ESTADO_GRAVACAO['buffer'].append(watts)
                logger.info(f"Ponto capturado: {watts}W")
        
        return jsonify({"ack": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# 2. INICIAR GRAVAÇÃO
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ESTADO_GRAVACAO
    if ESTADO_GRAVACAO['status'] in ["AGUARDANDO_GATILHO", "GRAVANDO"]:
        return jsonify({"erro": "Ocupado"}), 409

    data = request.get_json()
    nome_aparelho = data.get('nome_aparelho', 'Desconhecido')
    ESTADO_GRAVACAO['aparelho_alvo'] = nome_aparelho
    
    bg_thread = threading.Thread(target=worker_gravacao, args=(app.app_context(), nome_aparelho))
    bg_thread.start()
    
    return jsonify({"mensagem": "Iniciado"}), 202

# 3. CONSULTAR STATUS
@app.route('/api/status_gravacao', methods=['GET'])
def status_gravacao():
    return jsonify(ESTADO_GRAVACAO), 200

@app.route('/api/listar_assinaturas', methods=['GET'])
def listar_assinaturas():
    assinaturas = AssinaturaAparelho.query.all()
    lista = []
    for a in assinaturas:
        lista.append({
            "id": a.id,
            "nome": a.nome_aparelho,
            "pontos": json.loads(a.dados_json),
            "data": a.data_criacao
        })
    return jsonify(lista), 200

# 4. IDENTIFICAR APARELHO (AGORA ESTÁ NO LUGAR CERTO)
@app.route('/api/identificar', methods=['GET'])
def identificar():
    global ESTADO_GRAVACAO
    
    leitura_atual = ESTADO_GRAVACAO.get('ultima_leitura', 0.0)
    
    if leitura_atual < 5.0:
        return jsonify({
            "identificado": "Nenhum aparelho detectado",
            "confianca": "Alta",
            "watts_atuais": leitura_atual
        }), 200

    try:
        assinaturas = AssinaturaAparelho.query.all()
        melhor_match = "Desconhecido"
        menor_diferenca = float('inf')
        
        for assinatura in assinaturas:
            pontos = json.loads(assinatura.dados_json)
            if len(pontos) > 0:
                media_aparelho = sum(pontos) / len(pontos)
                diferenca = abs(leitura_atual - media_aparelho)
                
                if diferenca < menor_diferenca:
                    menor_diferenca = diferenca
                    melhor_match = assinatura.nome_aparelho

        limite_tolerancia = 50.0 
        
        if menor_diferenca > limite_tolerancia:
            return jsonify({
                "identificado": "Desconhecido / Não Cadastrado",
                "detalhe": f"Parece {melhor_match}, mas dif={menor_diferenca:.1f}W",
                "watts_atuais": leitura_atual
            }), 200
            
        return jsonify({
            "identificado": melhor_match,
            "diferenca": menor_diferenca,
            "watts_atuais": leitura_atual
        }), 200

    except Exception as e:
        logger.error(f"Erro na identificação: {e}")
        return jsonify({"erro": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
import os
import time
import json
import logging
from datetime import datetime, timedelta
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

# --- VARIÁVEIS GLOBAIS (ESTRATÉGIA DA FILA) ---
# BUFFER_DADOS: A "caixa" onde guardamos os dados durante a gravação
BUFFER_DADOS = []       
# GRAVANDO_AGORA: A "chave" que diz se devemos guardar os dados ou jogar fora
GRAVANDO_AGORA = False  
# ULTIMA_LEITURA: Usado apenas para o gatilho (saber se ligou)
ULTIMA_LEITURA = 0.0    

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
        logger.info(f"Banco de dados inicializado em: {db_path}")
    except Exception as e:
        logger.error(f"Erro ao criar tabelas: {e}")

# --- ROTAS ---

@app.route('/')
def home():
    return "API de Reconhecimento de Energia - Status: ONLINE (Modo Buffer 10 Pontos)", 200

@app.route('/api/setup_db', methods=['GET'])
def setup_db():
    try:
        with app.app_context():
            db.create_all()
        return jsonify({"message": "Tabelas recriadas com sucesso."}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# 1. RECEBE DADOS DO ESP32
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ULTIMA_LEITURA, BUFFER_DADOS, GRAVANDO_AGORA
    
    try:
        data = request.get_json()
        
        # Pega os dados (padrão 0.0 se falhar)
        watts = float(data.get('watts', data.get('power', 0.0)))
        corrente = float(data.get('corrente', 0.0))
        
        # 1. Atualiza a leitura instantânea (para o gatilho ver)
        ULTIMA_LEITURA = watts
        logger.info(f"Recebido: {watts}W")

        # 2. ESTRATÉGIA DO BUFFER: 
        # Se a gravação estiver ativa, joga esse dado na caixa!
        if GRAVANDO_AGORA:
            BUFFER_DADOS.append(watts)
            logger.info(f"--> [GRAVANDO] Ponto capturado: {len(BUFFER_DADOS)}/10")

        # 3. Salva no Banco (Histórico opcional, mantive seu código original)
        nova_leitura = LeituraTempoReal(corrente=corrente, watts=watts)
        db.session.add(nova_leitura)
        db.session.commit()
        
        return jsonify({"status": "recebido", "watts": watts}), 200

    except Exception as e:
        logger.error(f"Erro no data_stream: {e}")
        return jsonify({"erro": str(e)}), 500

# 2. GRAVAR ASSINATURA (CORRIGIDO: SISTEMA DE FILA/BUFFER)
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ULTIMA_LEITURA, BUFFER_DADOS, GRAVANDO_AGORA
    
    try:
        dados_req = request.get_json()
        nome_aparelho = dados_req.get('nome_aparelho', 'Desconhecido')
        
        logger.info(f"--- INICIANDO GRAVAÇÃO PARA: {nome_aparelho} ---")

        # 1. RESET: Limpa a caixa e trava a gravação
        GRAVANDO_AGORA = False
        BUFFER_DADOS = []

        # 2. GATILHO: Espera o aparelho ligar (> 30W)
        logger.info("Aguardando sinal acima de 30W...")
        start_wait = time.time()
        
        while True:
            # Timeout de segurança (2 minutos esperando ligar)
            if (time.time() - start_wait) > 120: 
                return jsonify({"erro": "Tempo limite excedido. O aparelho não foi ligado."}), 400
            
            # Verifica o gatilho na variável atualizada pelo data_stream
            if ULTIMA_LEITURA > 30.0:
                logger.info(f"GATILHO DETECTADO: {ULTIMA_LEITURA}W")
                break
                
            time.sleep(0.5) # Checa a cada meio segundo

        # 3. GRAVAÇÃO: Abre a comporta e espera encher 10 pontos
        logger.info("GATILHO ACIONADO! COLETANDO 10 PONTOS...")
        GRAVANDO_AGORA = True # <--- AQUI O ESP32 COMEÇA A ENCHER A LISTA
        
        start_collect = time.time()
        
        # Fica preso aqui até ter 10 pontos na lista
        while len(BUFFER_DADOS) < 10:
            
            # Timeout de segurança (se a internet cair no meio e parar de chegar dados)
            if (time.time() - start_collect) > 60: 
                GRAVANDO_AGORA = False
                return jsonify({
                    "erro": f"Falha na coleta. Consegui apenas {len(BUFFER_DADOS)} pontos.",
                    "dados_parciais": BUFFER_DADOS
                }), 400
            
            time.sleep(0.5) # Dorme um pouquinho enquanto a lista enche

        # 4. FINALIZAÇÃO
        GRAVANDO_AGORA = False # Fecha a comporta
        
        # Faz uma cópia dos dados para salvar
        valores_capturados = list(BUFFER_DADOS)
        logger.info(f"COLETA CONCLUÍDA! Dados: {valores_capturados}")

        # Salva a assinatura definitiva no Banco
        nova_assinatura = AssinaturaAparelho(
            nome_aparelho=nome_aparelho,
            dados_json=json.dumps(valores_capturados)
        )
        db.session.add(nova_assinatura)
        db.session.commit()

        # Limpa o buffer para a próxima vez
        BUFFER_DADOS = []

        return jsonify({
            "mensagem": f"Sucesso! Assinatura de '{nome_aparelho}' salva.",
            "pontos_capturados": len(valores_capturados),
            "dados": valores_capturados
        }), 200

    except Exception as e:
        # Garante que destrava em caso de erro
        GRAVANDO_AGORA = False 
        logger.error(f"Erro fatal ao gravar: {e}")
        return jsonify({"erro": str(e)}), 500

@app.route('/api/listar_assinaturas', methods=['GET'])
def listar_assinaturas():
    assinaturas = AssinaturaAparelho.query.all()
    lista = []
    for a in assinaturas:
        lista.append({
            "id": a.id,
            "nome": a.nome_aparelho,
            "data": a.data_criacao
        })
    return jsonify(lista), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
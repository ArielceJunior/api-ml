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
# Como estamos usando threads, precisamos de um dicionário para gerenciar o estado
ESTADO_GRAVACAO = {
    "status": "OCIOSO", # OCIOSO, AGUARDANDO_GATILHO, GRAVANDO, CONCLUIDO, ERRO
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

# --- FUNÇÃO WORKER (O CÉREBRO QUE RODA EM SEGUNDO PLANO) ---
def worker_gravacao(app_context, nome_aparelho):
    """
    Esta função roda separada do servidor principal.
    Ela fica monitorando as variáveis globais sem travar a API.
    """
    global ESTADO_GRAVACAO
    
    # Precisamos do contexto da aplicação para acessar o Banco de Dados dentro da Thread
    with app_context:
        logger.info(f"[THREAD] Iniciando monitoramento para: {nome_aparelho}")
        
        start_wait = time.time()
        
        # FASE 1: Esperar Gatilho (> 30W)
        ESTADO_GRAVACAO['status'] = "AGUARDANDO_GATILHO"
        ESTADO_GRAVACAO['buffer'] = [] # Limpa buffer
        
        gatilho_acionado = False
        
        while True:
            # Timeout de espera (2 min)
            if (time.time() - start_wait) > 120:
                ESTADO_GRAVACAO['status'] = "ERRO"
                ESTADO_GRAVACAO['mensagem'] = "Timeout: Aparelho não ligou em 120s."
                return

            # Verifica a leitura atual (atualizada pela rota data_stream)
            if ESTADO_GRAVACAO['ultima_leitura'] > 30.0:
                gatilho_acionado = True
                break
            
            time.sleep(0.2) # Dorme pouco para não gastar CPU

        # FASE 2: Gravação
        logger.info("[THREAD] Gatilho acionado! Gravando...")
        ESTADO_GRAVACAO['status'] = "GRAVANDO"
        start_collect = time.time()

        # O loop de coleta agora apenas espera o buffer encher
        # Quem enche o buffer é a rota /data_stream
        while len(ESTADO_GRAVACAO['buffer']) < 10:
            
            # Timeout de coleta (se a internet cair)
            if (time.time() - start_collect) > 60:
                ESTADO_GRAVACAO['status'] = "ERRO"
                ESTADO_GRAVACAO['mensagem'] = "Timeout durante a coleta dos pontos."
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
            ESTADO_GRAVACAO['mensagem'] = f"Sucesso! {len(valores_finais)} pontos gravados."
            logger.info("[THREAD] Gravação concluída com sucesso.")
            
            # Reset após alguns segundos (opcional, para limpar status)
            # time.sleep(10)
            # ESTADO_GRAVACAO['status'] = "OCIOSO"
            
        except Exception as e:
            ESTADO_GRAVACAO['status'] = "ERRO"
            ESTADO_GRAVACAO['mensagem'] = f"Erro ao salvar no banco: {str(e)}"


# --- ROTAS ---

@app.route('/')
def home():
    return "API Async - Status: ONLINE", 200

# 1. RECEBE DADOS (ALTA FREQUÊNCIA)
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ESTADO_GRAVACAO
    
    try:
        data = request.get_json()
        watts = float(data.get('watts', 0.0))
        corrente = float(data.get('corrente', 0.0))
        
        # 1. Atualiza a "foto" atual para a Thread ver
        ESTADO_GRAVACAO['ultima_leitura'] = watts

        # 2. Se a Thread estiver na fase GRAVANDO, guardamos o dado
        if ESTADO_GRAVACAO['status'] == "GRAVANDO":
            # Evita buffer overflow se o ESP mandar muito rápido
            if len(ESTADO_GRAVACAO['buffer']) < 10:
                ESTADO_GRAVACAO['buffer'].append(watts)
                logger.info(f"Ponto capturado: {watts}W")

        # (Opcional) Salvar histórico geral no banco
        # Se estiver muito lento, comente as 3 linhas abaixo
        # nova_leitura = LeituraTempoReal(corrente=corrente, watts=watts)
        # db.session.add(nova_leitura)
        # db.session.commit()
        
        return jsonify({"ack": True}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# 2. INICIAR GRAVAÇÃO (NÃO BLOQUEANTE)
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ESTADO_GRAVACAO

    # Se já estiver ocupado, rejeita
    if ESTADO_GRAVACAO['status'] in ["AGUARDANDO_GATILHO", "GRAVANDO"]:
        return jsonify({"erro": "Já existe uma gravação em andamento."}), 409

    data = request.get_json()
    nome_aparelho = data.get('nome_aparelho', 'Desconhecido')

    # Configura o estado inicial
    ESTADO_GRAVACAO['aparelho_alvo'] = nome_aparelho
    ESTADO_GRAVACAO['mensagem'] = "Iniciando monitoramento..."
    
    # --- A MÁGICA ACONTECE AQUI ---
    # Criamos uma thread que roda a função worker_gravacao em paralelo
    # Passamos o 'app' original para ele poder abrir conexão com o banco
    bg_thread = threading.Thread(target=worker_gravacao, args=(app.app_context(), nome_aparelho))
    bg_thread.start()
    
    # Retorna IMEDIATAMENTE. Não espera o aparelho ligar.
    return jsonify({
        "mensagem": "Monitoramento iniciado. Ligue o aparelho agora.",
        "status_url": "/api/status_gravacao" # O front deve consultar essa URL
    }), 202

# 3. CONSULTAR STATUS (POLLING)
# Como a gravação é assíncrona, seu front/postman deve chamar essa rota
# a cada 2 segundos para saber se acabou.
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
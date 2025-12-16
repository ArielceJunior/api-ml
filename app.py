import os
import time
import json
import logging
import threading
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

# Controle da Gravação Assíncrona
ESTADO_GRAVACAO = {
    "status": "OCIOSO", 
    "mensagem": "Nenhuma gravação em andamento",
    "buffer": [],
    "ultima_leitura": 0.0,
    "aparelho_alvo": ""
}

# Controle da Identificação (Janela Deslizante)
BUFFER_IDENTIFICACAO = []   # Guarda os últimos 5 valores
TAMANHO_JANELA = 5          # Quantidade de pontos para fazer a média
APARELHO_ATUAL = "Desconhecido" # O veredito do sistema no momento
ULTIMA_MEDIA = 0.0          # Média calculada da janela

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


# --- LÓGICA DE INTELIGÊNCIA (IDENTIFICAÇÃO) ---
def processar_identificacao(media_atual):
    """
    Compara a média atual (dos ultimos 5 segundos) com as assinaturas do banco.
    """
    global APARELHO_ATUAL
    
    # 1. Se for muito baixo, nem perde tempo procurando no banco
    if media_atual < 10.0:
        APARELHO_ATUAL = "Desligado / Standby"
        return

    try:
        # Pega todas as assinaturas salvas
        assinaturas = AssinaturaAparelho.query.all()
        
        melhor_match = "Desconhecido"
        menor_diferenca = float('inf')
        
        for assinatura in assinaturas:
            # Recupera os dados salvos
            pontos_salvos = json.loads(assinatura.dados_json)
            if not pontos_salvos: continue
            
            # Média da assinatura salva
            media_salva = sum(pontos_salvos) / len(pontos_salvos)
            
            # Compara com a média atual
            diferenca = abs(media_atual - media_salva)
            
            # Lógica: O quão perto precisa estar? (Ex: +/- 25W)
            if diferenca < 25.0 and diferenca < menor_diferenca:
                menor_diferenca = diferenca
                melhor_match = assinatura.nome_aparelho
        
        # Atualiza o veredito global
        if melhor_match != "Desconhecido":
            APARELHO_ATUAL = melhor_match
            # logger.info(f"Identificado: {melhor_match} (Diff: {menor_diferenca:.1f})")
        else:
            APARELHO_ATUAL = "Desconhecido"

    except Exception as e:
        logger.error(f"Erro ao identificar: {e}")


# --- FUNÇÃO WORKER (THREAD - GRAVAÇÃO) ---
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
            
            # Gatilho: espera subir acima de 30W
            if ESTADO_GRAVACAO['ultima_leitura'] > 30.0:
                break
            time.sleep(0.2)

        # FASE 2: Gravação
        logger.info("[THREAD] Gatilho acionado! Gravando...")
        ESTADO_GRAVACAO['status'] = "GRAVANDO"
        start_collect = time.time()

        # Espera o buffer encher (quem enche é o data_stream)
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
    return "API Inteligente - Status: ONLINE", 200

# 1. RECEBE DADOS (ESP32 -> API)
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ESTADO_GRAVACAO, BUFFER_IDENTIFICACAO, ULTIMA_MEDIA
    
    try:
        data = request.get_json()
        watts = float(data.get('watts', 0.0))
        
        # Atualiza variável rápida para a Thread de Gravação ler
        ESTADO_GRAVACAO['ultima_leitura'] = watts

        # --- LÓGICA 1: ALIMENTAR GRAVAÇÃO (SE HOUVER) ---
        if ESTADO_GRAVACAO['status'] == "GRAVANDO":
            if len(ESTADO_GRAVACAO['buffer']) < 10:
                ESTADO_GRAVACAO['buffer'].append(watts)
                logger.info(f"Ponto de Gravação: {watts}W")

        # --- LÓGICA 2: ALIMENTAR IDENTIFICAÇÃO (JANELA DESLIZANTE) ---
        BUFFER_IDENTIFICACAO.append(watts)
        
        # Mantém a janela apenas com 5 itens (FIFO)
        if len(BUFFER_IDENTIFICACAO) > TAMANHO_JANELA:
            BUFFER_IDENTIFICACAO.pop(0)

        # Se a janela está cheia, calcula média e tenta identificar
        if len(BUFFER_IDENTIFICACAO) == TAMANHO_JANELA:
            media = sum(BUFFER_IDENTIFICACAO) / TAMANHO_JANELA
            ULTIMA_MEDIA = media # Guarda para o front ver
            processar_identificacao(media)

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

# 3. CONSULTAR STATUS DA GRAVAÇÃO
@app.route('/api/status_gravacao', methods=['GET'])
def status_gravacao():
    return jsonify(ESTADO_GRAVACAO), 200

# 4. CONSULTAR STATUS EM TEMPO REAL (IDENTIFICAÇÃO)
# O Frontend deve chamar essa rota a cada 1s ou 2s
@app.route('/api/status_atual', methods=['GET'])
def status_atual():
    global APARELHO_ATUAL, ESTADO_GRAVACAO, ULTIMA_MEDIA
    return jsonify({
        "watts_instantaneo": ESTADO_GRAVACAO['ultima_leitura'],
        "watts_media_janela": ULTIMA_MEDIA,
        "aparelho_identificado": APARELHO_ATUAL
    }), 200

# 5. LISTAR ASSINATURAS SALVAS
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
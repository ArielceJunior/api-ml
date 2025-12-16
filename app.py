import os
import time
import json
import logging
import threading
import numpy as np                 # <--- MATEMÁTICA
from sklearn.neighbors import KNeighborsClassifier # <--- INTELIGÊNCIA ARTIFICIAL
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

# Controle da Identificação
BUFFER_IDENTIFICACAO = []   
TAMANHO_JANELA = 5          
APARELHO_ATUAL = "Desconhecido" 
ULTIMA_MEDIA = 0.0 

# --- CÉREBRO DA IA (ESTADO) ---
MODELO_IA = None       # Aqui ficará o objeto treinado (KNN)
MAPA_NOMES = {}        # Dicionário para traduzir ID -> Nome (Ex: 0 -> "Secador")

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
        logger.info(f"Banco de dados inicializado.")
    except Exception as e:
        logger.error(f"Erro DB: {e}")

# ==============================================================================
#  1. O CORAÇÃO DA IA: EXTRAÇÃO DE CARACTERÍSTICAS
#  Transforma dados brutos em "impressão digital" estatística
# ==============================================================================
def extrair_features(lista_pontos):
    """
    Entrada: [220, 222, 218, 220, 225]
    Saída:   [221.0, 2.5, 225, 218] (Média, Desvio, Max, Min)
    """
    if not lista_pontos: return [0,0,0,0]
    
    arr = np.array(lista_pontos)
    return [
        np.mean(arr), # Média (Potência)
        np.std(arr),  # Desvio Padrão (Estabilidade/Ruído)
        np.max(arr),  # Pico
        np.min(arr)   # Vale
    ]

# ==============================================================================
#  2. O TREINAMENTO: ENSINANDO A IA COM DADOS DO BANCO
# ==============================================================================
def treinar_ia():
    global MODELO_IA, MAPA_NOMES
    
    with app.app_context():
        assinaturas = AssinaturaAparelho.query.all()
    
    # Se não tem dados, não tem como treinar
    if len(assinaturas) < 1:
        logger.warning("IA: Sem dados suficientes para treinar.")
        MODELO_IA = None
        return

    X = [] # Features (Números que descrevem o aparelho)
    y = [] # Labels (IDs dos aparelhos)
    
    # Mapeia nomes para números (A IA só entende números)
    nomes_unicos = list(set([a.nome_aparelho for a in assinaturas]))
    MAPA_NOMES = {i: nome for i, nome in enumerate(nomes_unicos)}
    mapa_reverso = {nome: i for i, nome in MAPA_NOMES.items()} # Nome -> ID

    for a in assinaturas:
        pontos = json.loads(a.dados_json)
        features = extrair_features(pontos) # <--- APLICANDO A ESTATÍSTICA
        
        X.append(features)
        y.append(mapa_reverso[a.nome_aparelho])

    # Cria e treina o classificador KNN
    # n_neighbors=1: Procura o vizinho mais próximo exato
    knn = KNeighborsClassifier(n_neighbors=1) 
    knn.fit(X, y)
    
    MODELO_IA = knn
    logger.info(f"IA TREINADA! Conhece {len(nomes_unicos)} aparelhos.")

# Treina uma vez ao iniciar o servidor
treinar_ia()

# ==============================================================================
#  3. A PREDIÇÃO: USANDO A IA EM TEMPO REAL
# ==============================================================================
def processar_identificacao(buffer_atual):
    global APARELHO_ATUAL, MODELO_IA
    
    # Filtro básico: Se for muito baixo, nem pergunta pra IA
    media_temp = sum(buffer_atual)/len(buffer_atual)
    if media_temp < 10.0:
        APARELHO_ATUAL = "Desligado"
        return

    if MODELO_IA is None:
        APARELHO_ATUAL = "IA Não Treinada"
        return

    try:
        # 1. Extrai as features do que está acontecendo AGORA
        features_agora = extrair_features(buffer_atual)
        
        # 2. Pergunta pra IA: "Quem é esse?"
        # reshape(1, -1) é exigência do sklearn para prever 1 único item
        id_previsto = MODELO_IA.predict([features_agora])[0]
        
        # 3. Verifica a distância (Confiança)
        # Se o aparelho estiver muito longe matematicamente, é "Desconhecido"
        distancias, _ = MODELO_IA.kneighbors([features_agora])
        distancia = distancias[0][0]
        
        if distancia > 50.0: # Tolerância vetorial (ajustável)
            APARELHO_ATUAL = "Desconhecido"
        else:
            APARELHO_ATUAL = MAPA_NOMES.get(id_previsto, "Erro")

    except Exception as e:
        logger.error(f"Erro IA: {e}")
        APARELHO_ATUAL = "Erro IA"


# --- FUNÇÃO WORKER (GRAVAÇÃO) ---
def worker_gravacao(app_context, nome_aparelho):
    global ESTADO_GRAVACAO
    with app_context:
        logger.info(f"[THREAD] Monitorando: {nome_aparelho}")
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
        logger.info("[THREAD] Gravando...")
        ESTADO_GRAVACAO['status'] = "GRAVANDO"
        start_collect = time.time()

        while len(ESTADO_GRAVACAO['buffer']) < 10:
            if (time.time() - start_collect) > 60:
                ESTADO_GRAVACAO['status'] = "ERRO"
                ESTADO_GRAVACAO['mensagem'] = "Timeout coleta."
                return
            time.sleep(0.1)

        # FASE 3: Salvar e Re-treinar
        try:
            valores_finais = list(ESTADO_GRAVACAO['buffer'])
            nova = AssinaturaAparelho(
                nome_aparelho=nome_aparelho,
                dados_json=json.dumps(valores_finais)
            )
            db.session.add(nova)
            db.session.commit()
            
            # --- PULO DO GATO: RE-TREINAR A IA AGORA ---
            logger.info("Nova assinatura salva. Atualizando cérebro da IA...")
            treinar_ia() 
            # -------------------------------------------
            
            ESTADO_GRAVACAO['status'] = "CONCLUIDO"
            ESTADO_GRAVACAO['mensagem'] = "Sucesso! IA Atualizada."
            
        except Exception as e:
            ESTADO_GRAVACAO['status'] = "ERRO"
            ESTADO_GRAVACAO['mensagem'] = f"Erro: {str(e)}"

# --- ROTAS DA API ---

@app.route('/')
def home():
    return "API Inteligente (KNN) - Status: ONLINE", 200

@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ESTADO_GRAVACAO, BUFFER_IDENTIFICACAO, ULTIMA_MEDIA
    try:
        data = request.get_json()
        watts = float(data.get('watts', data.get('power', 0.0)))
        ESTADO_GRAVACAO['ultima_leitura'] = watts

        # Lógica de Gravação
        if ESTADO_GRAVACAO['status'] == "GRAVANDO" and len(ESTADO_GRAVACAO['buffer']) < 10:
            ESTADO_GRAVACAO['buffer'].append(watts)

        # Lógica de Identificação (Janela Deslizante)
        BUFFER_IDENTIFICACAO.append(watts)
        if len(BUFFER_IDENTIFICACAO) > TAMANHO_JANELA:
            BUFFER_IDENTIFICACAO.pop(0)

        if len(BUFFER_IDENTIFICACAO) == TAMANHO_JANELA:
            processar_identificacao(BUFFER_IDENTIFICACAO) # Chama a IA
            ULTIMA_MEDIA = sum(BUFFER_IDENTIFICACAO) / TAMANHO_JANELA

        return jsonify({"ack": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ESTADO_GRAVACAO
    if ESTADO_GRAVACAO['status'] in ["AGUARDANDO_GATILHO", "GRAVANDO"]:
        return jsonify({"erro": "Ocupado"}), 409

    data = request.get_json()
    nome = data.get('nome_aparelho', 'Desconhecido')
    ESTADO_GRAVACAO['aparelho_alvo'] = nome
    
    bg_thread = threading.Thread(target=worker_gravacao, args=(app.app_context(), nome))
    bg_thread.start()
    return jsonify({"mensagem": "Iniciado"}), 202

@app.route('/api/status_gravacao', methods=['GET'])
def status_gravacao():
    return jsonify(ESTADO_GRAVACAO), 200

@app.route('/api/status_atual', methods=['GET'])
def status_atual():
    global APARELHO_ATUAL, ESTADO_GRAVACAO, ULTIMA_MEDIA
    return jsonify({
        "watts_instantaneo": ESTADO_GRAVACAO['ultima_leitura'],
        "watts_media_janela": ULTIMA_MEDIA,
        "aparelho_identificado": APARELHO_ATUAL
    }), 200

@app.route('/api/debug/db', methods=['GET'])
def debug_db():
    try:
        assinaturas = AssinaturaAparelho.query.all()
        lista = []
        for a in assinaturas:
            lista.append({
                "id": a.id, 
                "nome": a.nome_aparelho, 
                "pontos": json.loads(a.dados_json),
                "data": a.data_criacao
            })
        return jsonify({"total": len(lista), "dados": lista}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/api/listar_assinaturas', methods=['GET'])
def listar_assinaturas():
    return debug_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
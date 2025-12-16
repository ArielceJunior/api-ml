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

# --- VARIÁVEIS GLOBAIS (CACHE RAM) ---
# Aqui fica a última leitura recebida, acessível instantaneamente
ULTIMA_LEITURA_CACHE = {
    "watts": 0.0,
    "timestamp": 0
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
        logger.info(f"Banco de dados inicializado em: {db_path}")
    except Exception as e:
        logger.error(f"Erro ao criar tabelas: {e}")

# --- ROTAS ---

@app.route('/')
def home():
    return "API de Reconhecimento de Energia - Status: ONLINE (Modo RAM Ativado)", 200

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
    global ULTIMA_LEITURA_CACHE
    try:
        data = request.get_json()
        
        # Pega os dados (padrão 0.0 se falhar)
        watts = float(data.get('watts', 0.0))
        corrente = float(data.get('corrente', 0.0))
        
        # 1. Atualiza a Memória RAM (Instantâneo)
        ULTIMA_LEITURA_CACHE['watts'] = watts
        ULTIMA_LEITURA_CACHE['timestamp'] = time.time()
        
        logger.info(f"Recebido: {watts}W")

        # 2. Salva no Banco (Histórico opcional, pode ser removido se quiser mais performance)
        nova_leitura = LeituraTempoReal(corrente=corrente, watts=watts)
        db.session.add(nova_leitura)
        db.session.commit()
        
        return jsonify({"status": "recebido", "watts": watts}), 200

    except Exception as e:
        logger.error(f"Erro no data_stream: {e}")
        return jsonify({"erro": str(e)}), 500

# 2. GRAVAR ASSINATURA (CORRIGIDO PARA LER DA RAM)
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ULTIMA_LEITURA_CACHE
    
    try:
        dados = request.get_json()
        nome_aparelho = dados.get('nome_aparelho', 'Desconhecido')
        
        logger.info(f"Iniciando gravação para: {nome_aparelho}")

        # --- FASE 1: GATILHO (ESPERAR LIGAR > 30W) ---
        start_wait = time.time()
        sinal_detectado = False
        
        while (time.time() - start_wait) < 300: # 5 min timeout
            # Lê da RAM
            potencia_atual = ULTIMA_LEITURA_CACHE['watts']
            
            # Verifica se o dado é recente (menos de 5 seg)
            dado_recente = (time.time() - ULTIMA_LEITURA_CACHE['timestamp']) < 5

            if dado_recente and potencia_atual > 30.0:
                logger.info(f"GATILHO! {potencia_atual}W. Gravando...")
                sinal_detectado = True
                break
            
            time.sleep(0.5)

        if not sinal_detectado:
            return jsonify({"erro": "Tempo limite excedido. Aparelho não ligado."}), 400

        # --- FASE 2: GRAVAÇÃO DA CURVA (A CORREÇÃO ESTÁ AQUI) ---
        # Em vez de esperar 16s e ler do banco, lemos da RAM em tempo real
        
        valores_lidos = []
        inicio_gravacao = time.time()
        
        logger.info("Capturando dados da RAM...")

        # Grava por 5 segundos
        while (time.time() - inicio_gravacao) < 5:
            # Pega o valor atual da RAM
            valor_atual = ULTIMA_LEITURA_CACHE['watts']
            valores_lidos.append(valor_atual)
            
            # Espera 0.5s para pegar o próximo ponto (ESP manda a cada 1s aprox)
            time.sleep(0.5)
            
        logger.info(f"Gravação concluída. Pontos: {len(valores_lidos)}")

        # Verifica se capturou algo
        if not valores_lidos:
             return jsonify({"erro": "Nenhum dado recebido durante a gravação."}), 500

        # Salva a assinatura definitiva no Banco
        nova_assinatura = AssinaturaAparelho(
            nome_aparelho=nome_aparelho,
            dados_json=json.dumps(valores_lidos) # Salva o array direto
        )
        db.session.add(nova_assinatura)
        db.session.commit()

        return jsonify({
            "mensagem": f"Sucesso! Assinatura de '{nome_aparelho}' salva.",
            "pontos_capturados": len(valores_lidos),
            "dados": valores_lidos
        }), 200

    except Exception as e:
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
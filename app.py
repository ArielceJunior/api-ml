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

# Configuração de Logs (para vermos o que acontece na Azure)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- BANCO DE DADOS (AZURE FIX) ---
# Usa a pasta /home na Azure para não perder dados, ou /tmp localmente
home_dir = os.environ.get('HOME', os.path.abspath(os.path.dirname(__file__)))
db_path = os.path.join(home_dir, 'assinaturas.db')

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- CORS (Permite conexão com o Vercel) ---
CORS(app, resources={r"/api/*": {"origins": "*"}})

# --- VARIÁVEIS GLOBAIS (O SEGREDO DA VELOCIDADE) ---
# Armazena a última leitura na memória RAM para acesso instantâneo
ULTIMA_LEITURA_CACHE = {
    "watts": 0.0,
    "timestamp": 0
}

# --- MODELOS DO BANCO DE DADOS ---
class LeituraTempoReal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    corrente = db.Column(db.Float, nullable=False)
    watts = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)

class AssinaturaAparelho(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_aparelho = db.Column(db.String(100), nullable=False)
    dados_json = db.Column(db.Text, nullable=False) # Array de watts salvo como texto
    data_criacao = db.Column(db.DateTime, default=datetime.now)

# Cria as tabelas na inicialização (Tentativa automática)
with app.app_context():
    try:
        db.create_all()
        logger.info(f"Banco de dados inicializado em: {db_path}")
    except Exception as e:
        logger.error(f"Erro ao criar tabelas na inicialização: {e}")

# --- ROTAS ---

@app.route('/')
def home():
    return "API de Reconhecimento de Energia - Status: ONLINE", 200

# 1. Rota de Emergência para criar tabelas
@app.route('/api/setup_db', methods=['GET'])
def setup_db():
    try:
        with app.app_context():
            db.create_all()
        return jsonify({"message": f"SUCESSO! Tabelas recriadas em {db_path}"}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# 2. Recebe dados do ESP32 (Stream)
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    global ULTIMA_LEITURA_CACHE
    try:
        data = request.get_json()
        
        # Pega os dados com segurança (padrao 0.0 se falhar)
        watts = float(data.get('watts', 0.0))
        corrente = float(data.get('corrente', 0.0)) # Se você mandar corrente tbm
        
        # 1. Atualiza o Cache Rápido (Memória RAM)
        ULTIMA_LEITURA_CACHE['watts'] = watts
        ULTIMA_LEITURA_CACHE['timestamp'] = time.time()
        
        logger.info(f"Recebido: {watts}W")

        # 2. Salva no Banco (Histórico)
        nova_leitura = LeituraTempoReal(corrente=corrente, watts=watts)
        db.session.add(nova_leitura)
        db.session.commit()
        
        return jsonify({"status": "recebido", "watts": watts}), 200

    except Exception as e:
        logger.error(f"Erro no data_stream: {e}")
        return jsonify({"erro": str(e)}), 500

# 3. Gravar Assinatura (O PROCESSO CRÍTICO)
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    global ULTIMA_LEITURA_CACHE
    
    try:
        dados = request.get_json()
        nome_aparelho = dados.get('nome_aparelho', 'Desconhecido')
        
        logger.info(f"Iniciando gravação para: {nome_aparelho}")

        # --- FASE 1: ESPERAR O GATILHO (> 50W) ---
        # Timeout de 5 minutos (300 segundos)
        start_wait = time.time()
        sinal_detectado = False
        
        while (time.time() - start_wait) < 300:
            # Lê direto da memória (MUITO RÁPIDO)
            potencia_atual = ULTIMA_LEITURA_CACHE['watts']
            
            # Verifica se o dado é recente (menos de 5 segundos)
            # Isso evita ler um dado velho travado na memória
            dado_recente = (time.time() - ULTIMA_LEITURA_CACHE['timestamp']) < 5

            if dado_recente and potencia_atual > 50.0:
                logger.info(f"GATILHO DETECTADO! Potência: {potencia_atual}W")
                sinal_detectado = True
                break
            
            time.sleep(0.5) # Checa a cada meio segundo

        if not sinal_detectado:
            return jsonify({"erro": "Tempo limite excedido. Aparelho não foi ligado."}), 400

        # --- FASE 2: GRAVAR DADOS (5 SEGUNDOS) ---
        logger.info("Gravando 5 segundos de dados...")
        
        # Marca a hora do trigger
        hora_gatilho = datetime.now()
        
        # Espera 5 segundos para o ESP32 enviar dados suficientes
        time.sleep(16) # 16 segundos para garantir 15 amostras (1 por segundo)
        
        # Busca no banco tudo que chegou DEPOIS do gatilho
        leituras = LeituraTempoReal.query.filter(
            LeituraTempoReal.timestamp >= hora_gatilho
        ).order_by(LeituraTempoReal.timestamp.asc()).all()
        
        if not leituras:
            return jsonify({"erro": "Nenhum dado recebido durante a gravação."}), 500

        # Extrai apenas os watts para um array
        array_watts = [l.watts for l in leituras]
        
        # Salva a assinatura definitiva
        nova_assinatura = AssinaturaAparelho(
            nome_aparelho=nome_aparelho,
            dados_json=json.dumps(array_watts)
        )
        db.session.add(nova_assinatura)
        db.session.commit()

        return jsonify({
            "mensagem": f"Sucesso! Assinatura de '{nome_aparelho}' gravada.",
            "pontos_capturados": len(array_watts),
            "dados": array_watts
        }), 200

    except Exception as e:
        logger.error(f"Erro ao gravar assinatura: {e}")
        return jsonify({"erro": str(e)}), 500

# 4. Rota para Listar Aparelhos (Para teste)
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
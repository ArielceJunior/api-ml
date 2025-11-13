import os
import time
import datetime
import joblib
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sklearn.ensemble import RandomForestClassifier
from flask_cors import CORS # Importa o CORS

# --- 1. Configuração do App e Banco de Dados ---
app = Flask(__name__)
# Define o caminho do banco de dados (assume que está na mesma pasta)
db_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'assinaturas.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
db = SQLAlchemy(app)

with app.app_context():
        db.create_all()

origins = [
    "http://localhost:5173",                
    "http://localhost:3000",                 
    "https://react-app-ml.vercel.app" 
]

# Configura o CORS para permitir apenas esses 'amigos'
CORS(app, resources={r"/api/*": {"origins": origins}})

# --- 2. Modelos do Banco de Dados ---
class LeituraTempoReal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    potencia_w = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class AssinaturaTreinamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_aparelho = db.Column(db.String(80), nullable=False)
    p_max = db.Column(db.Float, nullable=False)
    p_media = db.Column(db.Float, nullable=False)
    p_std = db.Column(db.Float, nullable=False) # Desvio Padrão
    tempo_ativo = db.Column(db.Float, nullable=False)

# --- 3. A Mágica da IA: Extrator de Features ---
def extrair_features(leituras_onda):
    onda = pd.Series([l.potencia_w for l in leituras_onda])
    onda_ativa = onda[onda > 5.0] # Filtro de ruído (ignora potências < 5W)
    if len(onda_ativa) < 2: return None # Precisa de pelo menos 2 pontos de dados
    features = {
        'p_max': np.max(onda_ativa),
        'p_media': np.mean(onda_ativa),
        'p_std': np.std(onda_ativa),
        'tempo_ativo': len(onda_ativa)
    }
    return features

# --- 4. Carregamento do Modelo de IA ---
MODELO_IA_ARQUIVO = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'modelo_aparelhos.pkl')
modelo_ia = None
if os.path.exists(MODELO_IA_ARQUIVO):
    try:
        modelo_ia = joblib.load(MODELO_IA_ARQUIVO)
        print(f"Modelo de IA '{MODELO_IA_ARQUIVO}' carregado com sucesso!")
    except Exception as e:
        print(f"Erro ao carregar o modelo: {e}")
else:
    print(f"AVISO: Modelo '{MODELO_IA_ARQUIVO}' não encontrado. Rode 'train.py' para criá-lo.")

# --- 5. APIs (Endpoints) ---

# API 1: Recebe dados do ESP8266 (ou do simulador)
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    data = request.get_json(silent=True) # Adicione silent=True para evitar 400s automáticos do Flask
    
    if data is None:
        return jsonify(success=False, error="Conteúdo JSON mal formatado."), 400

    # AGORA BUSCA A CHAVE CORRETA: 'power'
    if 'power' in data:
        try:
            # Garante que o dado é um float antes de salvar
            potencia_valor = float(data['power'])
            
            nova_leitura = LeituraTempoReal(potencia_w=potencia_valor)
            db.session.add(nova_leitura)
            db.session.commit()
            
            # Imprime no terminal para ver que está recebendo
            print(f"Leitura recebida: {potencia_valor}W") 
            
            return jsonify(success=True)
        
        except ValueError:
            return jsonify(success=False, error="Potência não é um número válido."), 400

    # Se a chave 'power' não foi encontrada
    return jsonify(success=False, error="Chave 'power' ausente no JSON."), 400

# Função helper para a lógica de "escuta" inteligente
def esperar_e_gravar(max_espera_s=300, gravacao_s=10, limiar_w=50.0):
    # Limpa o buffer de leituras antigas
    db.session.query(LeituraTempoReal).delete()
    db.session.commit()
    
    print(f"MODO DE ESPERA: Aguardando sinal > {limiar_w}W...")
    
    start_time_espera = datetime.datetime.utcnow()
    trigger_time = None
    
    # Loop de Espera (Polling)
    while datetime.datetime.utcnow() - start_time_espera < datetime.timedelta(seconds=max_espera_s):
        # Procura por um sinal de ativação no banco de dados
        leitura_trigger = LeituraTempoReal.query.filter(
            LeituraTempoReal.timestamp > start_time_espera,
            LeituraTempoReal.potencia_w > limiar_w
        ).first()
        
        if leitura_trigger:
            print(f"SINAL DETECTADO! Potência: {leitura_trigger.potencia_w}W")
            trigger_time = leitura_trigger.timestamp
            break # Sai do loop de espera
        time.sleep(0.5) # Verifica o banco a cada 500ms

    # Se não encontrou sinal, retorna erro
    if trigger_time is None:
        print("Tempo limite de espera atingido.")
        return None, "Tempo limite de 5 min atingido. Nenhuma atividade detectada."

    # Se encontrou, espera a janela de gravação
    print(f"Gravando assinatura por {gravacao_s} segundos...")
    time.sleep(gravacao_s) 

    # Coleta os dados da janela de tempo correta
    inicio_janela = trigger_time - datetime.timedelta(seconds=2)
    fim_janela = trigger_time + datetime.timedelta(seconds=gravacao_s)
    
    leituras_capturadas = LeituraTempoReal.query.filter(
        LeituraTempoReal.timestamp >= inicio_janela,
        LeituraTempoReal.timestamp <= fim_janela
    ).order_by(LeituraTempoReal.timestamp).all()

    if len(leituras_capturadas) < 3:
        return None, "Erro ao capturar dados após o trigger."

    # Extrai as features
    features = extrair_features(leituras_capturadas)
    if features is None:
        return None, "Atividade muito fraca para medir."
        
    db.session.query(LeituraTempoReal).delete() # Limpa o buffer
    db.session.commit()
    return features, None # Retorna features (sucesso) e nenhum erro


# API 2: Usada pela página de Treinamento
@app.route('/api/gravar_assinatura', methods=['POST'])
def gravar_assinatura():
    data = request.get_json()
    nome_aparelho = data.get('nome')
    if not nome_aparelho:
        return jsonify(success=False, error="Nome do aparelho não fornecido"), 400

    features, error = esperar_e_gravar()
    if error:
        return jsonify(success=False, error=error), 400

    # Salva as features no banco de treinamento
    nova_assinatura = AssinaturaTreinamento(
        nome_aparelho=nome_aparelho,
        p_max=features['p_max'], p_media=features['p_media'],
        p_std=features['p_std'], tempo_ativo=features['tempo_ativo']
    )
    db.session.add(nova_assinatura)
    db.session.commit()
    
    print(f"Assinatura gravada para: {nome_aparelho} com features: {features}")
    return jsonify(success=True, features=features)


# API 3: Usada pela página de Identificação
@app.route('/api/identificar', methods=['GET'])
def identificar_aparelho():
    if modelo_ia is None:
        return jsonify(success=False, error="Modelo de IA não está treinado/carregado."), 500

    features, error = esperar_e_gravar()
    if error:
        return jsonify(success=False, error=error), 400

    # Prepara as features para o modelo
    df_features = pd.DataFrame([features], columns=['p_max', 'p_media', 'p_std', 'tempo_ativo'])
    
    # FAZ A PREDIÇÃO!
    predicao = modelo_ia.predict(df_features)
    probabilidade = modelo_ia.predict_proba(df_features)
    
    aparelho_identificado = predicao[0]
    confianca = np.max(probabilidade) * 100

    print(f"Resultado: {aparelho_identificado} ({confianca:.2f}%)")
    return jsonify(
        success=True, 
        aparelho=aparelho_identificado, 
        confianca=f"{confianca:.2f}%",
        features_detectadas=features
    )

# --- 6. Inicialização ---
if __name__ == '__main__':
     # Cria o arquivo 'assinaturas.db' se não existir
    # Roda o app na porta 5000, acessível por qualquer IP na rede
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
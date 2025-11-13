from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
import datetime
import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import RandomForestClassifier

# --- Configuração do App e Banco de Dados ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///assinaturas.db'
db = SQLAlchemy(app)

# --- Modelos do Banco de Dados ---

# Tabela 1: Armazena os dados brutos que chegam do ESP
class LeituraTempoReal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    potencia_w = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

# Tabela 2: Armazena as "features" (características) de cada aparelho
class AssinaturaTreinamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_aparelho = db.Column(db.String(80), nullable=False)
    p_max = db.Column(db.Float, nullable=False)
    p_media = db.Column(db.Float, nullable=False)
    p_std = db.Column(db.Float, nullable=False) # Desvio Padrão
    tempo_ativo = db.Column(db.Float, nullable=False)

# --- APIs (Endpoints) ---

# API 1: O ESP8266 envia dados para cá
@app.route('/api/data_stream', methods=['POST'])
def data_stream():
    data = request.get_json()
    if 'potencia_w' in data:
        # Salva a leitura no banco de dados
        nova_leitura = LeituraTempoReal(potencia_w=data['potencia_w'])
        db.session.add(nova_leitura)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(success=False, error="Dados inválidos"), 400

# --- Rotas do Site (Frontend) ---

# Rota 1: Página inicial (Ainda não vamos criar o HTML)
@app.route('/')
def index():
    return "Servidor de IA de Energia no Ar!"

# --- Inicialização ---
if __name__ == '__main__':
    # Cria o banco de dados se ele não existir
    with app.app_context():
        db.create_all()
    # Inicia o servidor
    app.run(host='0.0.0.0', port=5000, debug=True)
import pandas as pd
import joblib
import os
from sklearn.ensemble import RandomForestClassifier
from app import AssinaturaTreinamento, app # Importa do seu app.py

print("Iniciando script de treinamento...")

# Carrega os dados do banco de dados
with app.app_context():
    query = AssinaturaTreinamento.query.all()
    
    if len(query) < 2:
        print("Erro: Você precisa de pelo menos 2 amostras no banco para treinar.")
        print("Acesse a interface de treinamento e grave algumas assinaturas primeiro.")
        exit()
        
    # Converte os dados do banco para um DataFrame do pandas
    df = pd.DataFrame([(d.nome_aparelho, d.p_max, d.p_media, d.p_std, d.tempo_ativo) 
                       for d in query], 
                      columns=['nome_aparelho', 'p_max', 'p_media', 'p_std', 'tempo_ativo'])

print(f"Treinando modelo com {len(df)} amostras...")
print(df) # Mostra os dados que ele está usando

# Separa os dados de "features" (X) e "rótulos" (y)
features_list = ['p_max', 'p_media', 'p_std', 'tempo_ativo']
X = df[features_list] # As características
y = df['nome_aparelho'] # O que queremos prever

# Cria e treina o modelo de IA
modelo_ia = RandomForestClassifier(n_estimators=100, random_state=42)
modelo_ia.fit(X, y)

# Salva o modelo treinado em um arquivo
model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'modelo_aparelhos.pkl')
joblib.dump(modelo_ia, model_path)

print("\nTreinamento concluído!")
print(f"Modelo salvo em 'modelo_aparelhos.pkl'")
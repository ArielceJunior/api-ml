import requests
import time
import random

# URL do seu servidor (se estiver rodando no mesmo PC)
SERVER_URL = "https://ariflaskml-dff5g9c7bdhyhsc5.brazilsouth-01.azurewebsites.net/api/data_stream"

# Assinaturas "Falsas" que vamos simular
ASSINATURAS = {
    "liquidificador": [0, 0, 50, 800, 790, 810, 795, 805, 0, 0],
    "geladeira":      [0, 0, 0, 650, 150, 155, 149, 152, 0, 0, 0, 0, 0],
    "lampada":        [0, 0, 10, 10, 11, 10, 10, 10, 11, 0, 0],
    "microondas":     [0, 0, 15, 1300, 1290, 1310, 1300, 1305, 0, 0]
}

def enviar_assinatura(nome_aparelho):
    if nome_aparelho not in ASSINATURAS:
        print(f"Erro: Assinatura '{nome_aparelho}' não definida.")
        return

    onda = ASSINATURAS[nome_aparelho]
    print(f"Enviando assinatura '{nome_aparelho}' para {SERVER_URL}...")
    
    for potencia in onda:
        try:
            # Adiciona um pouco de ruído para ficar mais real
            potencia_ruido = potencia
            if potencia > 0:
                potencia_ruido = potencia + random.uniform(-2.0, 2.0)

            payload = {'potencia_w': potencia_ruido}
            response = requests.post(SERVER_URL, json=payload, timeout=2)
            print(f"  Enviado: {potencia_ruido:.2f} W ... Resposta: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"Erro ao conectar ao servidor: {e}\nVerifique se 'app.py' está rodando.")
            return
        
        time.sleep(1.0) # Espera 1 segundo, igual o ESP8266
    
    print("Envio da assinatura concluído.")

if __name__ == "__main__":
    print("--- Simulador do ESP8266 ---")
    print("Aparelhos disponíveis para simular:", list(ASSINATURAS.keys()))
    
    while True:
        nome = input("\nDigite o nome do aparelho para simular (ou 'sair'): ")
        if nome.lower() == 'sair':
            break
        if nome in ASSINATURAS:
            enviar_assinatura(nome)
        else:
            print("Aparelho não encontrado. Tente novamente.")
# Copie este arquivo para config.py e preencha com as suas credenciais.
# O config.py esta no .gitignore e nao deve ser publicado.
# Campos vazios desativam a integracao correspondente.

# OpenWeatherMap (https://openweathermap.org/api)
# Vazio: a pressao vem da Open-Meteo, que nao exige chave.
OWM_API_KEY = ""

# Google Sheets: URL do Web App do Apps Script (terminada em /exec)
# e o mesmo token definido no Code.gs
PLANILHA_URL = ""
PLANILHA_TOKEN = ""

# Email (SMTP do Gmail com senha de app)
EMAIL_REMETENTE = ""            # ex.: "seu_email@gmail.com"
EMAIL_SENHA_APP = ""            # senha de app de 16 letras gerada no Google
EMAIL_DESTINOS = []             # ex.: ["destino1@exemplo.com", "destino2@exemplo.com"]

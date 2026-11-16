import time
import math
import random
import gc
import socket
import network
import ntptime
import ubinascii
import framebuf
import dht
from machine import Pin, ADC, SoftI2C, unique_id
from umqtt.simple import MQTTClient
import ssd1306

try:
    import ujson as json
except ImportError:
    import json

try:
    import urequests as requests
except ImportError:
    import requests

# CREDENCIAIS
# Ficam no config.py, que nao vai para o repositorio (veja o config_exemplo.py).
# Sem o config.py, o exemplo e usado e as integracoes que dependem de
# credencial (OpenWeatherMap, Google Sheets e email) ficam desativadas.
try:
    from config import (OWM_API_KEY, PLANILHA_URL, PLANILHA_TOKEN,
                        EMAIL_REMETENTE, EMAIL_SENHA_APP, EMAIL_DESTINOS)
except ImportError:
    from config_exemplo import (OWM_API_KEY, PLANILHA_URL, PLANILHA_TOKEN,
                                EMAIL_REMETENTE, EMAIL_SENHA_APP, EMAIL_DESTINOS)

# CONFIGURACOES
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

# Broker MQTT publico da HiveMQ
MQTT_HOST = "broker.hivemq.com"
MQTT_PORT = 1883
TOPICO_BASE = "estacao/viniciusborba"

# OpenWeatherMap (chave no config.py)
OWM_CIDADE = "Sao Paulo,BR"

# API reserva sem chave (Open-Meteo)
LATITUDE = -23.55
LONGITUDE = -46.63

# Relogio (NTP) - horario de Brasilia
FUSO_HORARIO = -3

# Google Sheets (URL do Web App e token no config.py)
INTERVALO_PLANILHA = 30000              # grava uma leitura a cada 30 s
HTTP_TIMEOUT = 25                       # segundos de espera por resposta
DIAGNOSTICO_PLANILHA = True            # mostra a resposta do script (desligar depois)

# Email (SMTP Gmail com senha de app)
SMTP_SERVIDOR = "smtp.gmail.com"
SMTP_PORTA = 465
# remetente, senha de app e destinatarios no config.py
HORA_RELATORIO = (23, 59)               # envio automatico no fim do dia
USAR_RESUMO_PLANILHA = False            # True: pede a media tambem a planilha (2 conexoes HTTPS)
ARQUIVO_DIA = "dia.json"                # acumuladores do dia salvos na flash

# PINOS
PINO_DHT = 15
PINO_LDR = 34
PINO_GAS = 35
PINO_BOTAO = 4
PINO_SDA = 21
PINO_SCL = 22

# LIMITES DE ALERTA
LIM_TEMP_ALTA = 35.0    # C
LIM_TEMP_BAIXA = 5.0    # C
LIM_UMID_BAIXA = 20.0   # %
LIM_GAS_RUIM = 60.0     # % da escala do MQ-2
LIM_IC_PERIGO = 41.0    # indice de calor (C)

# INTERVALOS (ms)
INTERVALO_SENSORES = 2000
INTERVALO_MQTT = 5000
INTERVALO_API = 60000
INTERVALO_TELA = 250
TEMPO_BOTAO_LONGO = 1500   # segurar o botao 1,5 s envia o relatorio na hora

# HARDWARE
i2c = SoftI2C(sda=Pin(PINO_SDA), scl=Pin(PINO_SCL))
oled = ssd1306.SSD1306_I2C(128, 64, i2c)
sensor_dht = dht.DHT22(Pin(PINO_DHT))

adc_ldr = ADC(Pin(PINO_LDR))
adc_ldr.atten(ADC.ATTN_11DB)
adc_gas = ADC(Pin(PINO_GAS))
adc_gas.atten(ADC.ATTN_11DB)

botao = Pin(PINO_BOTAO, Pin.IN, Pin.PULL_UP)

# ESTADO
dados = {
    "temperatura": None, "umidade": None, "indiceCalor": None,
    "conforto": "--", "lux": 0, "luz": "--",
    "gasPct": 0.0, "qualidadeAr": "--", "alerta": "Nenhum",
}
alertas = []

TAM_HIST = 6
historico_pressao = []
pressao_atual = None
tendencia_pressao = 0.0
previsao = "Aguardando"
fonte_pressao = "--"

# acumuladores do dia (media calculada no ESP32)
dia_atual = None
soma_temp = 0.0
soma_umid = 0.0
qtd_leituras = 0
temp_min = temp_max = umid_min = umid_max = None
ultimo_relatorio = None
status_planilha = "--"
status_email = "--"
relogio_ok = False

NUM_TELAS = 6
tela_atual = 0
evento_botao = None
t_press = 0

wlan = network.WLAN(network.STA_IF)
mqtt = None
mqtt_ok = False
t_reconexao = 0


#   CALCULOS

def calcular_indice_calor(t_c, ur):
    """Indice de calor (conforto termico) - equacao de Rothfusz (NOAA)."""
    T = t_c * 1.8 + 32.0
    hi = 0.5 * (T + 61.0 + ((T - 68.0) * 1.2) + (ur * 0.094))

    if (hi + T) / 2.0 >= 80.0:
        hi = (-42.379 + 2.04901523 * T + 10.14333127 * ur
              - 0.22475541 * T * ur - 0.00683783 * T * T
              - 0.05481717 * ur * ur + 0.00122874 * T * T * ur
              + 0.00085282 * T * ur * ur - 0.00000199 * T * T * ur * ur)
        if ur < 13 and 80 <= T <= 112:
            hi -= ((13 - ur) / 4.0) * math.sqrt((17 - abs(T - 95.0)) / 17.0)
        elif ur > 85 and 80 <= T <= 87:
            hi += ((ur - 85) / 10.0) * ((87 - T) / 5.0)

    return (hi - 32.0) / 1.8


def classificar_conforto(ic, t):
    if t < 18:
        return "Frio"
    if ic < 27:
        return "Confortavel"
    if ic < 32:
        return "Cautela"
    if ic < 41:
        return "Cautela extrema"
    if ic < 54:
        return "Perigo"
    return "Perigo extremo"


def calcular_lux(raw):
    """Converte a leitura do modulo LDR em lux."""
    GAMMA, RL10 = 0.7, 50.0
    tensao = raw / 4095.0 * 3.3
    if tensao >= 3.29:
        return 0
    if tensao <= 0.01:
        return 100000
    resistencia = 2000.0 * tensao / (1.0 - tensao / 3.3)
    return math.pow(RL10 * 1e3 * math.pow(10, GAMMA) / resistencia, 1.0 / GAMMA)


def classificar_luz(lux):
    if lux < 10:
        return "Escuro"
    if lux < 500:
        return "Pouca luz"
    if lux < 10000:
        return "Claro"
    if lux < 50000:
        return "Sol"
    return "Sol intenso"


def classificar_ar(pct):
    if pct < 30:
        return "Boa"
    if pct < LIM_GAS_RUIM:
        return "Moderada"
    if pct < 85:
        return "Ruim"
    return "Perigosa"


def calcular_previsao(p, tend, t, ur):
    """Previsao: pressao (API) + tendencia + temp/umid (sensor)."""
    if p is None:
        return "Sem dados"
    t = t if t is not None else 25
    ur = ur if ur is not None else 50

    if p < 1000 and tend <= -1.0:
        return "Tempestade" if (t >= 25 and ur >= 70) else "Chuva forte"
    if tend <= -1.0:
        return "Chuva" if ur >= 75 else "Nublado, chuva prov."
    if p < 1009:
        return "Chuva" if ur >= 80 else "Nublado"
    if p <= 1018:
        if tend >= 0.5 and ur < 70:
            return "Ensolarado"
        return "Parcial. nublado"
    return "Ensolarado e quente" if t >= 30 else "Ensolarado"


#   RELOGIO

def sincronizar_relogio():
    global relogio_ok
    for _ in range(3):
        try:
            ntptime.settime()          # acerta o RTC em UTC
            relogio_ok = True
            print("Relogio sincronizado:", data_hora_txt())
            return True
        except Exception as e:
            print("NTP falhou:", e)
            time.sleep(1)
    return False


def agora_local():
    return time.localtime(time.time() + FUSO_HORARIO * 3600)


def data_hora_txt(t=None):
    t = t or agora_local()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*t[:6])


def data_txt(t=None):
    t = t or agora_local()
    return "{:04d}-{:02d}-{:02d}".format(t[0], t[1], t[2])


def data_br(data_iso):
    a, m, d = data_iso.split("-")
    return "{}/{}/{}".format(d, m, a)


#   SENSORES / ALERTAS

def ler_sensores():
    try:
        sensor_dht.measure()
        t = sensor_dht.temperature()
        h = sensor_dht.humidity()
        dados["temperatura"] = t
        dados["umidade"] = h
        dados["indiceCalor"] = calcular_indice_calor(t, h)
        dados["conforto"] = classificar_conforto(dados["indiceCalor"], t)
    except OSError as e:
        print("Erro DHT22:", e)

    dados["lux"] = calcular_lux(adc_ldr.read())
    dados["luz"] = classificar_luz(dados["lux"])

    dados["gasPct"] = adc_gas.read() * 100.0 / 4095.0
    dados["qualidadeAr"] = classificar_ar(dados["gasPct"])


def verificar_alertas():
    global alertas
    alertas = []
    t = dados["temperatura"]
    if t is not None:
        if t >= LIM_TEMP_ALTA:
            alertas.append("TEMP. ALTA!")
        if t <= LIM_TEMP_BAIXA:
            alertas.append("TEMP. BAIXA!")
        if dados["umidade"] <= LIM_UMID_BAIXA:
            alertas.append("AR MUITO SECO!")
        if dados["indiceCalor"] >= LIM_IC_PERIGO:
            alertas.append("CALOR PERIGOSO!")
    if dados["gasPct"] >= LIM_GAS_RUIM:
        alertas.append("AR POLUIDO/GAS!")
    dados["alerta"] = " | ".join(alertas) if alertas else "Nenhum"


#   REDE

def conectar_wifi():
    mensagem_oled("Conectando", "Wi-Fi...")
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    inicio = time.ticks_ms()
    while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), inicio) < 15000:
        time.sleep_ms(250)
        print(".", end="")
    print("\nWi-Fi OK" if wlan.isconnected() else "\nWi-Fi falhou")


def criar_cliente_mqtt():
    client_id = "esp32-estacao-" + ubinascii.hexlify(unique_id()).decode()
    return MQTTClient(client_id, MQTT_HOST, port=MQTT_PORT, keepalive=60)


def reconectar_mqtt():
    """Tenta conectar ao HiveMQ a cada 5 s, sem travar o programa."""
    global mqtt, mqtt_ok, t_reconexao
    if mqtt_ok or not wlan.isconnected():
        return
    if time.ticks_diff(time.ticks_ms(), t_reconexao) < 5000:
        return
    t_reconexao = time.ticks_ms()
    print("Conectando MQTT...", end=" ")
    try:
        gc.collect()
        mqtt = criar_cliente_mqtt()
        mqtt.connect()
        mqtt.publish((TOPICO_BASE + "/status").encode(), b"online", retain=True)
        mqtt_ok = True
        print("OK")
    except Exception as e:
        mqtt_ok = False
        print("falhou:", e)


def desconectar_mqtt():
    global mqtt_ok, t_reconexao
    if mqtt_ok:
        try:
            mqtt.disconnect()
        except Exception:
            pass
    mqtt_ok = False
    t_reconexao = time.ticks_add(time.ticks_ms(), -5000)   # reconecta logo em seguida


def publicar(topico, payload, retain=False):
    global mqtt_ok
    if not mqtt_ok:
        return
    try:
        msg = json.dumps(payload)
        mqtt.publish((TOPICO_BASE + topico).encode(), msg.encode(), retain=retain)
        print(topico, msg)
    except Exception as e:
        print("Erro ao publicar:", e)
        mqtt_ok = False


def arred(v, casas=1):
    return None if v is None else round(v, casas)


def publicar_dados():
    publicar("/dados", {
        "dataHora": data_hora_txt() if relogio_ok else None,
        "temperatura": arred(dados["temperatura"]),
        "umidade": arred(dados["umidade"]),
        "indiceCalor": arred(dados["indiceCalor"]),
        "conforto": dados["conforto"],
        "lux": int(dados["lux"]),
        "luz": dados["luz"],
        "gasPct": arred(dados["gasPct"]),
        "qualidadeAr": dados["qualidadeAr"],
        "alerta": dados["alerta"],
    })


def publicar_previsao():
    publicar("/previsao", {
        "previsao": previsao,
        "pressao": pressao_atual,
        "tendencia": arred(tendencia_pressao, 2),
        "temperatura": arred(dados["temperatura"]),
        "umidade": arred(dados["umidade"]),
        "fonte": fonte_pressao,
        "cidade": OWM_CIDADE,
    }, retain=True)


#   HTTP (APIs de clima e Google Sheets)

def http(metodo, url, corpo=None, seguir=True):
    """Faz GET/POST e devolve (codigo, json). Segue o redirecionamento
    que o Google Apps Script sempre responde (302)."""
    resp = None
    try:
        gc.collect()
        extras = {}
        if metodo == "POST":
            extras = {"data": corpo, "headers": {"Content-Type": "application/json"}}
        try:
            resp = requests.request(metodo, url, timeout=HTTP_TIMEOUT, **extras)
        except TypeError:
            # versoes antigas do urequests nao aceitam timeout
            resp = requests.request(metodo, url, **extras)
        cod = resp.status_code

        if cod in (301, 302, 303, 307, 308) and not seguir:
            return cod, None
        if cod in (301, 302, 303, 307, 308):
            destino = None
            for k, v in (getattr(resp, "headers", None) or {}).items():
                if k.lower() == "location":
                    destino = v
            resp.close()
            resp = None
            if destino:
                return http("GET", destino, seguir=False)
            return cod, None

        if cod == 200:
            try:
                return cod, resp.json()
            except ValueError:
                return cod, None
        print("HTTP", cod, url[:60])
        return cod, None
    except Exception as e:
        print("Falha HTTP:", e)
        return None, None
    finally:
        if resp:
            resp.close()


def https_get(url, max_redir=2):
    """GET HTTP/HTTPS feito direto com socket + ssl, mostrando cada etapa.
    Devolve (codigo, corpo_texto, cabecalhos)."""
    proto, _, resto = url.partition("://")
    host, _, caminho = resto.partition("/")
    caminho = "/" + caminho
    porta = 443 if proto == "https" else 80
    print("  [1] DNS", host)
    endereco = socket.getaddrinfo(host, porta)[0][-1]
    sock = socket.socket()
    sock.settimeout(HTTP_TIMEOUT)
    try:
        print("  [2] TCP", endereco)
        sock.connect(endereco)
        if proto == "https":
            print("  [3] TLS")
            import ssl
            if hasattr(ssl, "SSLContext"):
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=host)
            else:
                sock = ssl.wrap_socket(sock, server_hostname=host)
        print("  [4] enviando pedido")
        sock.write("GET {} HTTP/1.0\r\nHost: {}\r\nUser-Agent: ESP32\r\nConnection: close\r\n\r\n"
                   .format(caminho, host).encode())
        status = sock.readline().decode()
        codigo = int(status.split(" ")[1])
        print("  [5] resposta", codigo)
        cab = {}
        while True:
            linha = sock.readline()
            if not linha or linha == b"\r\n":
                break
            k, _, v = linha.decode().partition(":")
            cab[k.strip().lower()] = v.strip()
        corpo = b""
        if codigo == 200:
            while True:
                bloco = sock.read(512)
                if not bloco:
                    break
                corpo += bloco
    finally:
        sock.close()

    if codigo in (301, 302, 303, 307, 308) and max_redir > 0 and cab.get("location"):
        return codigo, None, cab
    texto = corpo.decode()
    if cab.get("transfer-encoding", "").lower() == "chunked":
        texto = desfazer_chunked(texto)
    return codigo, texto, cab


def desfazer_chunked(texto):
    saida, i = "", 0
    while i < len(texto):
        fim = texto.find("\r\n", i)
        if fim < 0:
            break
        tam = int(texto[i:fim].split(";")[0] or "0", 16)
        if tam == 0:
            break
        saida += texto[fim + 2:fim + 2 + tam]
        i = fim + 2 + tam + 2
    return saida


def google_get(url, seguir):
    """Chama o Web App. Se seguir=True, vai ate o redirecionamento para ler o JSON."""
    try:
        gc.collect()
        cod, texto, cab = https_get(url)
        if cod in (301, 302, 303, 307, 308):
            if not seguir:
                return cod, {"location": cab.get("location", "")}
            cod, texto, cab = https_get(cab["location"], max_redir=0)
        if cod == 200 and texto:
            try:
                return cod, json.loads(texto)
            except ValueError:
                return cod, None
        return cod, None
    except Exception as e:
        print("Falha HTTPS:", repr(e))
        return None, None


def buscar_pressao_api():
    """Pressao atmosferica (hPa) em tempo real: OpenWeatherMap e, se falhar, Open-Meteo."""
    if not wlan.isconnected():
        return None, None

    if OWM_API_KEY:
        cod, d = http("GET", "http://api.openweathermap.org/data/2.5/weather?q=" +
                      OWM_CIDADE.replace(" ", "%20") + "&appid=" +
                      OWM_API_KEY + "&units=metric")
        if d and "main" in d:
            return float(d["main"]["pressure"]), "OWM"

    cod, d = http("GET", "http://api.open-meteo.com/v1/forecast?latitude={}&longitude={}"
                  "&current=pressure_msl".format(LATITUDE, LONGITUDE))
    if d and "current" in d:
        return float(d["current"]["pressure_msl"]), "OpenMeteo"

    return None, None


def atualizar_previsao():
    global pressao_atual, tendencia_pressao, previsao, fonte_pressao
    p, fonte = buscar_pressao_api()
    if p is not None:
        fonte_pressao = fonte
        print("Pressao real ({}): {} hPa".format(fonte, p))
    else:
        base = pressao_atual if pressao_atual is not None else 1013.0
        p = round(base + random.randint(-15, 15) / 10.0, 1)
        fonte_pressao = "simulada"
    pressao_atual = p

    historico_pressao.append(p)
    if len(historico_pressao) > TAM_HIST:
        historico_pressao.pop(0)
    tendencia_pressao = p - historico_pressao[0] if len(historico_pressao) > 1 else 0.0

    previsao = calcular_previsao(pressao_atual, tendencia_pressao,
                                 dados["temperatura"], dados["umidade"])
    publicar_previsao()


def url_param(valor):
    """Codifica um valor para ir na URL (espaco e caracteres especiais)."""
    seguros = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
    return "".join(c if c in seguros else "%{:02X}".format(ord(c)) for c in str(valor))


def gravar_planilha(data_hora, temp, umid):
    """Envia uma leitura ao Web App (GET com parametros), que insere a linha no Google Sheets."""
    global status_planilha
    if not PLANILHA_URL or not PLANILHA_TOKEN:
        status_planilha = "desativada"
        return
    parametros = {
        "token": PLANILHA_TOKEN,
        "acao": "gravar",
        "dataHora": data_hora,
        "temperatura": round(temp, 1),
        "umidade": round(umid, 1),
        "indiceCalor": arred(dados["indiceCalor"]),
        "gasPct": arred(dados["gasPct"]),
    }
    url = PLANILHA_URL + "?" + "&".join(
        "{}={}".format(k, url_param(v)) for k, v in parametros.items())

    print("Gravando na planilha...")
    inicio = time.ticks_ms()
    # a linha ja e gravada na primeira resposta (302); nao precisa seguir o redirecionamento
    if DIAGNOSTICO_PLANILHA:
        print("URL:", url[:70] + "..." + url[-90:])
    cod, resp = google_get(url, seguir=DIAGNOSTICO_PLANILHA)
    if DIAGNOSTICO_PLANILHA:
        print("Resposta do script:", resp)
    print("Resposta da planilha: HTTP {} em {} ms".format(cod, time.ticks_diff(time.ticks_ms(), inicio)))
    destino = (resp or {}).get("location", "")
    if resp and resp.get("ok"):
        status_planilha = "OK L{}".format(resp.get("linha", "?"))
    elif resp and resp.get("erro"):
        status_planilha = "ERRO"
        print("Erro do script:", resp.get("erro"))
    elif cod == 302 and "script.googleusercontent.com" in destino:
        status_planilha = "OK"
    elif cod == 302:
        status_planilha = "ERRO acesso"
        print("Redirecionado para:", destino[:90])
        print("A implantacao do Apps Script precisa estar com acesso 'Qualquer pessoa'.")
    else:
        status_planilha = "ERRO {}".format(cod)
    print("Planilha:", status_planilha)


def resumo_planilha(data_iso):
    """Pede ao Web App a media do dia calculada a partir das linhas da planilha."""
    if not PLANILHA_URL or not PLANILHA_TOKEN:
        return None
    url = "{}?token={}&acao=resumo&data={}".format(PLANILHA_URL, PLANILHA_TOKEN, data_iso)
    print("Pedindo resumo do dia a planilha...")
    cod, resp = google_get(url, seguir=True)
    if resp and resp.get("ok") and resp.get("leituras"):
        return resp
    return None


#   MEDIA DIARIA

def salvar_dia():
    """Grava os acumuladores do dia na flash (sobrevivem a um reinicio)."""
    try:
        with open(ARQUIVO_DIA, "w") as f:
            json.dump({"dia": dia_atual, "somaT": soma_temp, "somaU": soma_umid,
                       "qtd": qtd_leituras, "tMin": temp_min, "tMax": temp_max,
                       "uMin": umid_min, "uMax": umid_max,
                       "ultimoRel": ultimo_relatorio}, f)
    except OSError as e:
        print("Erro ao salvar dia:", e)


def carregar_dia():
    global dia_atual, soma_temp, soma_umid, qtd_leituras, ultimo_relatorio
    global temp_min, temp_max, umid_min, umid_max
    try:
        with open(ARQUIVO_DIA) as f:
            d = json.load(f)
        dia_atual, soma_temp, soma_umid = d["dia"], d["somaT"], d["somaU"]
        qtd_leituras, ultimo_relatorio = d["qtd"], d.get("ultimoRel")
        temp_min, temp_max = d.get("tMin"), d.get("tMax")
        umid_min, umid_max = d.get("uMin"), d.get("uMax")
        print("Dia recuperado da flash: {} ({} leituras)".format(dia_atual, qtd_leituras))
    except (OSError, ValueError, KeyError):
        pass


def zerar_dia():
    global soma_temp, soma_umid, qtd_leituras, temp_min, temp_max, umid_min, umid_max
    soma_temp = soma_umid = 0.0
    qtd_leituras = 0
    temp_min = temp_max = umid_min = umid_max = None


def registrar_leitura():
    """Guarda a leitura no acumulador do dia e grava na planilha."""
    global dia_atual, soma_temp, soma_umid, qtd_leituras
    global temp_min, temp_max, umid_min, umid_max
    t, h = dados["temperatura"], dados["umidade"]
    if t is None or not relogio_ok:
        return

    agora = agora_local()
    hoje = data_txt(agora)

    # virou o dia: fecha o dia anterior antes de zerar
    if dia_atual is not None and hoje != dia_atual:
        if qtd_leituras > 0 and ultimo_relatorio != dia_atual:
            enviar_relatorio(dia_atual)
        zerar_dia()
    dia_atual = hoje

    soma_temp += t
    soma_umid += h
    qtd_leituras += 1
    temp_min = t if temp_min is None else min(temp_min, t)
    temp_max = t if temp_max is None else max(temp_max, t)
    umid_min = h if umid_min is None else min(umid_min, h)
    umid_max = h if umid_max is None else max(umid_max, h)
    salvar_dia()
    gravar_planilha(data_hora_txt(agora), t, h)


def montar_resumo(data_iso):
    """Media do dia calculada no ESP32 (valores acumulados durante o dia).
    Opcional: confere com a planilha quando USAR_RESUMO_PLANILHA = True."""
    if USAR_RESUMO_PLANILHA:
        r = resumo_planilha(data_iso)
        if r:
            r["fonte"] = "Google Sheets"
            return r
    if qtd_leituras == 0 or data_iso != dia_atual:
        return None
    return {
        "data": data_iso,
        "leituras": qtd_leituras,
        "mediaTemp": round(soma_temp / qtd_leituras, 2),
        "mediaUmid": round(soma_umid / qtd_leituras, 2),
        "tempMin": temp_min, "tempMax": temp_max,
        "umidMin": umid_min, "umidMax": umid_max,
        "fonte": "media calculada no ESP32 a partir das leituras do dia (também gravadas no Google Sheets)",
    }


#   EMAIL (SMTP GMAIL)

def b64(texto):
    return ubinascii.b2a_base64(texto.encode()).strip()


def smtp_comando(sock, comando, esperado, rotulo=None):
    """Envia um comando SMTP e confere o codigo de resposta."""
    if comando is not None:
        sock.write(comando + b"\r\n")
    while True:
        linha = sock.readline()
        if not linha:
            raise OSError("SMTP sem resposta")
        linha = linha.decode().strip()
        if len(linha) < 4 or linha[3] != "-":   # "250-" continua, "250 " termina
            break
    codigo = int(linha[:3])
    if codigo not in esperado:
        raise OSError("SMTP {}: {}".format(rotulo or comando, linha))
    return codigo


def enviar_email(assunto, corpo):
    endereco = socket.getaddrinfo(SMTP_SERVIDOR, SMTP_PORTA)[0][-1]
    sock = socket.socket()
    sock.settimeout(20)
    sock.connect(endereco)

    import ssl
    if hasattr(ssl, "SSLContext"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=SMTP_SERVIDOR)
    else:
        sock = ssl.wrap_socket(sock, server_hostname=SMTP_SERVIDOR)

    try:
        smtp_comando(sock, None, (220,), "conexao")
        smtp_comando(sock, b"EHLO esp32", (250,))
        smtp_comando(sock, b"AUTH LOGIN", (334,))
        smtp_comando(sock, b64(EMAIL_REMETENTE), (334,), "usuario")
        smtp_comando(sock, b64(EMAIL_SENHA_APP.replace(" ", "")), (235,), "senha")
        smtp_comando(sock, "MAIL FROM:<{}>".format(EMAIL_REMETENTE).encode(), (250,))
        for destino in EMAIL_DESTINOS:
            smtp_comando(sock, "RCPT TO:<{}>".format(destino).encode(), (250, 251))
        smtp_comando(sock, b"DATA", (354,))

        cabecalho = (
            "From: Estacao Meteorologica ESP32 <{}>\r\n"
            "To: {}\r\n"
            "Subject: =?UTF-8?B?{}?=\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=UTF-8\r\n"
            "Content-Transfer-Encoding: 8bit\r\n\r\n"
        ).format(EMAIL_REMETENTE, ", ".join(EMAIL_DESTINOS), b64(assunto).decode())
        texto = corpo.replace("\n", "\r\n").replace("\r\n.", "\r\n..")
        sock.write((cabecalho + texto + "\r\n").encode())
        smtp_comando(sock, b".", (250,), "fim da mensagem")
        smtp_comando(sock, b"QUIT", (221,))
    finally:
        sock.close()


def enviar_relatorio(data_iso):
    """Monta o relatorio do dia e envia por email (e tambem pelo MQTT)."""
    global ultimo_relatorio, status_email
    mensagem_oled("Gerando", "relatorio...")
    r = montar_resumo(data_iso)
    if not r:
        status_email = "sem dados"
        print("Relatorio: sem leituras para", data_iso)
        return

    assunto = "Relatório Diário - Estação Meteorológica - {}".format(data_br(data_iso))
    linhas = [
        "RELATÓRIO DIÁRIO - ESTAÇÃO METEOROLÓGICA (ESP32)",
        "",
        "Data de referência: {}".format(data_br(data_iso)),
        "Quantidade de leituras: {}".format(r["leituras"]),
        "Média da temperatura: {:.2f} °C".format(r["mediaTemp"]),
        "Média da umidade: {:.2f} %".format(r["mediaUmid"]),
    ]
    if r.get("tempMin") is not None:
        linhas.append("Temperatura mín./máx.: {} °C / {} °C".format(r["tempMin"], r["tempMax"]))
        linhas.append("Umidade mín./máx.: {} % / {} %".format(r["umidMin"], r["umidMax"]))
    linhas += [
        "",
        "Origem dos dados: {}".format(r["fonte"]),
        "Relatório gerado e enviado pelo ESP32 em {}.".format(data_hora_txt()),
    ]
    corpo = "\n".join(linhas)
    print(corpo)

    publicar("/relatorio", {
        "data": data_br(data_iso), "leituras": r["leituras"],
        "mediaTemp": r["mediaTemp"], "mediaUmid": r["mediaUmid"],
        "fonte": r["fonte"], "enviadoEm": data_hora_txt(),
    }, retain=True)

    if not (EMAIL_REMETENTE and EMAIL_SENHA_APP and EMAIL_DESTINOS):
        status_email = "desativado"
        ultimo_relatorio = data_iso
        salvar_dia()
        print("Email desativado: preencha o config.py")
        return

    mensagem_oled("Enviando", "email...")
    desconectar_mqtt()          # libera memoria para a conexao segura com o Gmail
    for tentativa in (1, 2):
        try:
            if not wlan.isconnected():
                conectar_wifi()
            gc.collect()
            print("Enviando email (tentativa {}), memoria livre: {} bytes".format(tentativa, gc.mem_free()))
            enviar_email(assunto, corpo)
            status_email = "enviado " + data_hora_txt()[11:16]
            ultimo_relatorio = data_iso
            salvar_dia()
            print("Email enviado para", EMAIL_DESTINOS)
            break
        except Exception as e:
            status_email = "ERRO"
            print("Falha no email:", repr(e))
            time.sleep(2)


def verificar_hora_relatorio():
    t = agora_local()
    hoje = data_txt(t)
    if (t[3], t[4]) >= HORA_RELATORIO and ultimo_relatorio != hoje and qtd_leituras > 0:
        enviar_relatorio(hoje)


#   DISPLAY (16 colunas x 8 linhas)

def mensagem_oled(l1, l2=""):
    oled.fill(0)
    oled.text(l1, 0, 20)
    oled.text(l2, 0, 32)
    oled.show()


def texto_grande(s, x, y, esc=2):
    w = len(s) * 8
    buf = framebuf.FrameBuffer(bytearray(w), w, 8, framebuf.MONO_HLSB)
    buf.fill(0)
    buf.text(s, 0, 0, 1)
    for py in range(8):
        for px in range(w):
            if buf.pixel(px, py):
                oled.fill_rect(x + px * esc, y + py * esc, esc, esc, 1)


def fmt(v, casas=1):
    return "--" if v is None else ("{:." + str(casas) + "f}").format(v)


def cabecalho(titulo):
    oled.text(titulo, 0, 0)
    status = ("W" if wlan.isconnected() else "-") + ("M" if mqtt_ok else "-")
    oled.text("{} {}/{}".format(status, tela_atual + 1, NUM_TELAS), 80, 0)
    oled.hline(0, 9, 128, 1)


def barra_alerta():
    if not alertas:
        return
    agora = time.ticks_ms()
    if (agora // 500) % 2 == 0:
        idx = (agora // 2000) % len(alertas)
        txt = alertas[idx][:16]
        oled.fill_rect(0, 54, 128, 10, 1)
        oled.text(txt, (128 - len(txt) * 8) // 2, 55, 0)


def tela_clima():
    cabecalho("CLIMA")
    texto_grande(fmt(dados["temperatura"]) + "C", 0, 13)
    texto_grande(fmt(dados["umidade"]) + "%", 0, 33)


def tela_conforto():
    cabecalho("CONFORTO")
    oled.text("Indice de calor", 0, 13)
    texto_grande(fmt(dados["indiceCalor"]) + "C", 0, 24)
    oled.text(dados["conforto"], 0, 44)


def tela_luz():
    cabecalho("LUZ")
    texto_grande(str(int(dados["lux"])), 0, 13)
    oled.text("lux", 0, 32)
    oled.text(dados["luz"], 0, 43)


def tela_ar():
    cabecalho("AR")
    oled.text("Gas MQ-2: {}%".format(int(dados["gasPct"])), 0, 13)
    oled.rect(0, 25, 128, 8, 1)
    oled.fill_rect(0, 25, int(dados["gasPct"] * 1.28), 8, 1)
    oled.text("Ar: " + dados["qualidadeAr"], 0, 40)


def tela_previsao():
    cabecalho("PREVISAO")
    oled.text("P:{}hPa {}".format(fmt(pressao_atual, 0), fonte_pressao[:3]), 0, 12)
    sinal = "+" if tendencia_pressao >= 0 else ""
    oled.text("Tend:{}{}".format(sinal, fmt(tendencia_pressao)), 0, 22)
    oled.text(previsao[:16], 0, 34)
    oled.text(previsao[16:32], 0, 44)


def tela_registros():
    cabecalho("DIA")
    if relogio_ok:
        t = agora_local()
        oled.text("{:02d}/{:02d} {:02d}:{:02d}:{:02d}".format(t[2], t[1], t[3], t[4], t[5]), 0, 12)
    else:
        oled.text("Sem relogio", 0, 12)
    oled.text("Leituras: {}".format(qtd_leituras), 0, 22)
    oled.text("Sheet:" + status_planilha[:10], 0, 32)
    oled.text("Mail:" + status_email[:11], 0, 42)


TELAS = (tela_clima, tela_conforto, tela_luz, tela_ar, tela_previsao, tela_registros)


def desenhar_tela():
    oled.fill(0)
    TELAS[tela_atual]()
    barra_alerta()
    oled.show()


#   BOTAO: toque curto troca a tela, segurar envia o relatorio

def irq_botao(pin):
    global evento_botao, t_press
    agora = time.ticks_ms()
    if pin.value() == 0:
        t_press = agora
    elif t_press:
        duracao = time.ticks_diff(agora, t_press)
        t_press = 0
        if duracao >= TEMPO_BOTAO_LONGO:
            evento_botao = "longo"
        elif duracao >= 30:
            evento_botao = "curto"


botao.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING, handler=irq_botao)


#   PROGRAMA PRINCIPAL

def main():
    global tela_atual, evento_botao

    carregar_dia()
    conectar_wifi()
    sincronizar_relogio()
    ler_sensores()
    verificar_alertas()
    reconectar_mqtt()
    atualizar_previsao()
    registrar_leitura()

    agora = time.ticks_ms()
    t_sens = t_mqtt = t_tela = t_api = t_planilha = t_ntp = agora

    while True:
        agora = time.ticks_ms()

        if not wlan.isconnected():
            conectar_wifi()
        reconectar_mqtt()

        if not relogio_ok and time.ticks_diff(agora, t_ntp) >= 60000:
            t_ntp = agora
            sincronizar_relogio()

        if evento_botao == "curto":
            evento_botao = None
            tela_atual = (tela_atual + 1) % NUM_TELAS
            desenhar_tela()
        elif evento_botao == "longo":
            evento_botao = None
            if relogio_ok:
                enviar_relatorio(data_txt())

        if time.ticks_diff(agora, t_sens) >= INTERVALO_SENSORES:
            t_sens = agora
            ler_sensores()
            verificar_alertas()

        if time.ticks_diff(agora, t_mqtt) >= INTERVALO_MQTT:
            t_mqtt = agora
            publicar_dados()

        if time.ticks_diff(agora, t_planilha) >= INTERVALO_PLANILHA:
            t_planilha = agora
            registrar_leitura()
            if relogio_ok:
                verificar_hora_relatorio()

        if time.ticks_diff(agora, t_api) >= INTERVALO_API:
            t_api = agora
            atualizar_previsao()

        if time.ticks_diff(agora, t_tela) >= INTERVALO_TELA:
            t_tela = agora
            desenhar_tela()

        time.sleep_ms(20)


main()
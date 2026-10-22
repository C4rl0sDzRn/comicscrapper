#!/usr/bin/env python3
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import threading
import concurrent.futures
import difflib
import xml.etree.ElementTree as ET
import zipfile
import logging
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Configuración del archivo de LOG
# Configuración del archivo de LOG (Compatible con RPi)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s', 
    handlers=[logging.FileHandler('comicscrapper.log', encoding='utf-8')]
)

COMICVINE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Kavita-Sync/8.0"

# Dependencias opcionales
try:
    from PIL import Image
    import imagehash
    CARATULAS_DISPONIBLE = True
except ImportError:
    CARATULAS_DISPONIBLE = False

try:
    from tqdm import tqdm
    TQDM_DISPONIBLE = True
except ImportError:
    TQDM_DISPONIBLE = False

try:
    import pytesseract
    OCR_DISPONIBLE = True
except ImportError:
    OCR_DISPONIBLE = False

# --- GLOBALES ---
CONFIG_GENERAL = {}
COMICVINE_API_KEY = ""
METRON_TOKEN = ""
DB_CONN = None

# CANDADOS PARA MULTIHILO
DB_LOCK = threading.Lock()
API_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()

def tprint(mensaje, imprimir_consola=True):
    """Escribe en el log y, opcionalmente, de forma segura en la consola."""
    logging.info(mensaje.strip())
    if imprimir_consola:
        with PRINT_LOCK:
            print(mensaje)

def extraer_texto_portada_ocr(ruta_imagen):
    """OCR de emergencia si el hash de la carátula falla."""
    if not OCR_DISPONIBLE or not ruta_imagen: return ""
    try:
        with Image.open(ruta_imagen) as img:
            texto = pytesseract.image_to_string(img)
            return texto.strip()
    except Exception:
        return ""
# ==========================================
# CONFIGURACIÓN Y TAREAS INICIALES
# ==========================================
def cargar_configuracion():
    config_path = "config.json"
    if not os.path.exists(config_path):
        print("⚠️ No se encuentra 'config.json'. Por favor, ejecuta 'python3 configurar.py' primero.")
        sys.exit(1)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            global CONFIG_GENERAL, COMICVINE_API_KEY, METRON_TOKEN
            CONFIG_GENERAL = config
            apikeys = config.get("api_keys", {})
            COMICVINE_API_KEY = apikeys.get("comicvine", "")
            METRON_TOKEN = apikeys.get("metron", "")
            return config
    except Exception as e:
        print(f"❌ Error leyendo config.json: {e}")
        sys.exit(1)

def limpiar_basura(directorio):
    tprint(f"  🧹 Limpiando archivos basura en: {directorio}...")
    basura = ['.DS_Store', 'Thumbs.db', '@eaDir', 'desktop.ini']
    for root, dirs, files in os.walk(directorio, topdown=False):
        for f in files:
            if f in basura or f.startswith('._') or f.lower().endswith(('.tmp', '.filepart')):
                try: os.remove(os.path.join(root, f))
                except OSError: pass

def realizar_copia_seguridad_db(directorio):
    db_path = os.path.join(directorio, ".comicscrapper.sqlite")
    bak_path = os.path.join(directorio, ".comicscrapper.sqlite.bak")
    if os.path.exists(db_path):
        tprint("  💾 Guardando copia de seguridad de la base de datos...")
        try: shutil.copy2(db_path, bak_path)
        except OSError: pass

def iniciar_db(directorio):
    global DB_CONN
    db_path = os.path.join(directorio, ".comicscrapper.sqlite")
    DB_CONN = sqlite3.connect(db_path, check_same_thread=False)
    cursor = DB_CONN.cursor()

    with DB_LOCK:
        cursor.execute('CREATE TABLE IF NOT EXISTS archivos (ruta_relativa TEXT PRIMARY KEY, cv_id TEXT, mtime REAL)')
        cursor.execute('CREATE TABLE IF NOT EXISTS api_cache (url TEXT PRIMARY KEY, respuesta TEXT, timestamp REAL)')
        cursor.execute('''CREATE TABLE IF NOT EXISTS series (
                            ruta_carpeta TEXT PRIMARY KEY,
                            cv_id TEXT, mt_id TEXT, nombre_oficial TEXT
                          )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS alias_aprendidos (
                            nombre_normalizado TEXT PRIMARY KEY,
                            cv_id TEXT, mt_id TEXT, nombre_oficial TEXT,
                            veces_usado INTEGER DEFAULT 1
                          )''')

        cursor.execute("PRAGMA table_info(series)")
        columnas_existentes = {fila[1] for fila in cursor.fetchall()}
        columnas_nuevas = {
            "metodo": "TEXT",
            "fecha_asignacion": "REAL",
            "total_intentos": "INTEGER DEFAULT 0",
            "total_fallos": "INTEGER DEFAULT 0",
            "sospechosa": "INTEGER DEFAULT 0",
            "pendiente_confirmar": "INTEGER DEFAULT 0",
        }
        for col, tipo in columnas_nuevas.items():
            if col not in columnas_existentes:
                cursor.execute(f"ALTER TABLE series ADD COLUMN {col} {tipo}")
        DB_CONN.commit()

def obtener_ids_carpeta(ruta_carpeta, directorio_base):
    ruta_relativa = os.path.relpath(ruta_carpeta, directorio_base)
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT cv_id, mt_id FROM series WHERE ruta_carpeta = ?", (ruta_relativa,))
        row = cursor.fetchone()
    if row and (row[0] or row[1]): return row[0], row[1]

    json_path = os.path.join(ruta_carpeta, ".comic_id.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                cv_id, mt_id = data.get("cv_id"), data.get("mt_id")
                if cv_id or mt_id:
                    guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_id, mt_id,
                                               data.get("serie", ""), data.get("volumen"), metodo="json_previo")
                    return str(cv_id) if cv_id else None, str(mt_id) if mt_id else None
        except (json.JSONDecodeError, IOError): pass

    m_cv = re.findall(r"\[cv-(\d+)\]", os.path.basename(ruta_carpeta), re.I)
    m_mt = re.findall(r"\[mt-(\d+)\]", os.path.basename(ruta_carpeta), re.I)
    if m_cv or m_mt:
        cv_val = str(m_cv[-1]) if m_cv else None
        mt_val = str(m_mt[-1]) if m_mt else None
        guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_val, mt_val,
                                   limpiar_nombre_serie(os.path.basename(ruta_carpeta)), None, metodo="tag_legacy")
        return cv_val, mt_val
    return None, None

def guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_id, mt_id, serie, volumen, metodo="desconocido"):
    json_path = os.path.join(ruta_carpeta, ".comic_id.json")
    data = {"serie": serie, "volumen": volumen, "cv_id": cv_id, "mt_id": mt_id, "metodo": metodo}
    try:
        with open(json_path, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)
    except IOError: pass

    ruta_relativa = os.path.relpath(ruta_carpeta, directorio_base)
    pendiente = 1 if metodo == "caratula" else 0
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("""REPLACE INTO series
                           (ruta_carpeta, cv_id, mt_id, nombre_oficial, metodo, fecha_asignacion,
                            total_intentos, total_fallos, sospechosa, pendiente_confirmar)
                           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?)""",
                       (ruta_relativa, cv_id, mt_id, serie, metodo, time.time(), pendiente))
        DB_CONN.commit()

def registrar_intento_resolucion(ruta_carpeta, directorio_base, exito):
    ruta_relativa = os.path.relpath(ruta_carpeta, directorio_base)
    UMBRAL_MUESTRA_MINIMA = 5
    UMBRAL_FALLO = 0.35
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT total_intentos, total_fallos, sospechosa FROM series WHERE ruta_carpeta = ?", (ruta_relativa,))
        row = cursor.fetchone()
        if not row: return
        intentos, fallos, ya_sospechosa = (row[0] or 0) + 1, (row[1] or 0) + (1 if not exito else 0), row[2]
        nueva_sospecha = ya_sospechosa
        if intentos >= UMBRAL_MUESTRA_MINIMA and (fallos / intentos) > UMBRAL_FALLO:
            nueva_sospecha = 1
        cursor.execute("UPDATE series SET total_intentos = ?, total_fallos = ?, sospechosa = ? WHERE ruta_carpeta = ?",
                       (intentos, fallos, nueva_sospecha, ruta_relativa))
        DB_CONN.commit()
    if nueva_sospecha and not ya_sospechosa:
        tprint(f"  ⚠️  '{ruta_relativa}' marcada como SOSPECHOSA: {fallos}/{intentos} issues sin encontrar "
               f"con el ID actual. Revísala con 'comicscrapper doctor'.")

def limpiar_nombre_busqueda(texto):
    t = texto.lower()
    t = re.sub(r'&', 'and', t)
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    return ' '.join(t.split())

def buscar_alias_aprendido(nombre_serie):
    clave = limpiar_nombre_busqueda(nombre_serie)
    if not clave: return None, None, None

    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT cv_id, mt_id, nombre_oficial FROM alias_aprendidos WHERE nombre_normalizado = ?", (clave,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE alias_aprendidos SET veces_usado = veces_usado + 1 WHERE nombre_normalizado = ?", (clave,))
            DB_CONN.commit()
            return row[0], row[1], row[2]

        cursor.execute("SELECT nombre_normalizado, cv_id, mt_id, nombre_oficial FROM alias_aprendidos")
        todos = cursor.fetchall()
    if todos:
        claves = [t[0] for t in todos]
        parecidos = difflib.get_close_matches(clave, claves, n=1, cutoff=0.88)
        if parecidos:
            for t in todos:
                if t[0] == parecidos[0]:
                    return t[1], t[2], t[3]
    return None, None, None

def guardar_alias_aprendido(nombre_serie, cv_id, mt_id, nombre_oficial):
    clave = limpiar_nombre_busqueda(nombre_serie)
    if not clave: return
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("""INSERT INTO alias_aprendidos (nombre_normalizado, cv_id, mt_id, nombre_oficial)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(nombre_normalizado) DO UPDATE SET
                               cv_id=excluded.cv_id, mt_id=excluded.mt_id, nombre_oficial=excluded.nombre_oficial""",
                       (clave, cv_id, mt_id, nombre_oficial))
        DB_CONN.commit()

EDITORIALES_CONOCIDAS = [
    "marvel", "dc", "dc comics", "image", "image comics", "dark horse",
    "idw", "boom", "boom studios", "valiant", "dynamite", "vertigo",
    "wildstorm", "aftershock", "oni press", "archie", "dstlry",
]

def detectar_editorial_por_ruta(ruta_carpeta, directorio_base):
    try:
        rel = os.path.relpath(ruta_carpeta, directorio_base)
    except ValueError:
        return None
    for parte in rel.split(os.sep):
        parte_norm = parte.lower().strip()
        if parte_norm in EDITORIALES_CONOCIDAS:
            return parte_norm
    return None

def generar_variantes_busqueda(nombre_puro, numero_volumen=None):
    variantes = []
    
    if numero_volumen:
        variantes.append(f"{nombre_puro} vol {numero_volumen}")
        variantes.append(f"{nombre_puro} volume {numero_volumen}")
        
    variantes.append(nombre_puro)

    sin_subtitulo = re.split(r"\s*[:\-–]\s+", nombre_puro)[0].strip()
    if sin_subtitulo and sin_subtitulo.lower() != nombre_puro.lower():
        if numero_volumen:
            variantes.append(f"{sin_subtitulo} vol {numero_volumen}")
        variantes.append(sin_subtitulo)

    palabras = [p for p in re.split(r"\s+", sin_subtitulo or nombre_puro) if p]
    if len(palabras) > 2:
        variante_corta = " ".join(palabras[:2])
        if variante_corta.lower() not in [v.lower() for v in variantes]:
            if numero_volumen:
                variantes.append(f"{variante_corta} vol {numero_volumen}")
            variantes.append(variante_corta)

    return list(dict.fromkeys(variantes))

def decodificar_leetspeak(texto):
    if not re.search(r'[a-zA-Z]', texto): return texto
    mapa = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't'}
    return "".join([mapa.get(c, c) for c in texto])

# ==========================================
# MOTOR DE BÚSQUEDA Y CACHÉ
# ==========================================
def determinar_ttl_cache(url):
    if "field_list=issues" in url or "/api/issue/?series_id=" in url or "/api/issue/?arc=" in url: return 7
    if "/api/search/" in url or "/api/series/?name=" in url: return 90
    return 180

def peticion_api_segura(url, max_reintentos=4, dias_cache=None):
    global DB_CONN
    if dias_cache is None: dias_cache = determinar_ttl_cache(url)
    
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
        row = cursor.fetchone()
        if row and (time.time() - row[1]) < (dias_cache * 86400):
            return json.loads(row[0])

    pausa_config = CONFIG_GENERAL.get("rendimiento", {}).get("pausa_entre_peticiones_api", 1.5)

    with API_LOCK:
        with DB_LOCK:
            cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row and (time.time() - row[1]) < (dias_cache * 86400):
                return json.loads(row[0])

        tprint(f"  🌐 [CV] Consultando API…")
        for intento in range(max_reintentos):
            try:
                time.sleep(pausa_config)
                req = Request(url, headers={"User-Agent": COMICVINE_USER_AGENT})
                with urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                    if str(resp.get("error", "OK")).upper() != "OK": return resp

                    resultados = resp.get("results")
                    if resultados:
                        with DB_LOCK:
                            cursor.execute("REPLACE INTO api_cache (url, respuesta, timestamp) VALUES (?, ?, ?)",
                                           (url, json.dumps(resp), time.time()))
                            DB_CONN.commit()
                    return resp
            except HTTPError as e:
                if e.code == 429:
                    tprint(f"    ⏳ Rate-limit. Esperando 10 s (intento {intento+1}/{max_reintentos})…")
                    time.sleep(10)
                else: time.sleep(3)
            except Exception: time.sleep(3)
        return None

def peticion_api_metron(url, max_reintentos=3, dias_cache=None):
    global DB_CONN
    if not METRON_TOKEN: return None
    if dias_cache is None: dias_cache = determinar_ttl_cache(url)
    
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
        row = cursor.fetchone()
        if row and (time.time() - row[1]) < (dias_cache * 86400):
            return json.loads(row[0])

    pausa_config = CONFIG_GENERAL.get("rendimiento", {}).get("pausa_entre_peticiones_api", 1.5)

    with API_LOCK:
        with DB_LOCK:
            cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row and (time.time() - row[1]) < (dias_cache * 86400): return json.loads(row[0])

        tprint(f"  🌐 [Metron] Consultando API…")
        for intento in range(max_reintentos):
            try:
                time.sleep(pausa_config)
                req = Request(url, headers={"User-Agent": COMICVINE_USER_AGENT, "Authorization": f"Bearer {METRON_TOKEN}"})
                with urlopen(req, timeout=10) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                    tiene_datos = ("results" in resp and resp["results"]) or ("id" in resp)
                    if tiene_datos:
                        with DB_LOCK:
                            cursor.execute("REPLACE INTO api_cache (url, respuesta, timestamp) VALUES (?, ?, ?)",
                                           (url, json.dumps(resp), time.time()))
                            DB_CONN.commit()
                    return resp
            except Exception: time.sleep(2)
        return None

def descargar_caratula_hq(url_imagen):
    if not url_imagen: return None
    try:
        req = Request(url_imagen, headers={"User-Agent": COMICVINE_USER_AGENT})
        with urlopen(req, timeout=15) as r:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            temp_file.write(r.read())
            temp_file.close()
            return temp_file.name
    except Exception: return None

def nombres_candidato(v, origen):
    nombres = [v.get("name", "")]
    if origen == "cv":
        alias_raw = v.get("aliases") or ""
        nombres.extend(a.strip() for a in alias_raw.split("\n") if a.strip())
    return [n for n in nombres if n]

def candidato_coincide_texto(v, query_limpia, origen, exacto=False):
    for nombre in nombres_candidato(v, origen):
        nombre_limpio = limpiar_nombre_busqueda(nombre)
        if exacto and nombre_limpio == query_limpia: return True
        if not exacto and query_limpia in nombre_limpio: return True
    return False

def issue_existe_en_candidato(candidato_id, numero_issue, origen):
    if not numero_issue: return True
    if origen == "cv": mapa, mapa_ann, mapa_spc, _, _, _ = obtener_datos_volumen(candidato_id)
    else: mapa, mapa_ann, mapa_spc, _, _, _ = obtener_datos_volumen_metron(candidato_id)
    num = str(numero_issue).lstrip("0") or "0"
    return num in mapa or num in mapa_ann or num in mapa_spc

def procesar_candidatos(resultados, query, numero_volumen, total_comics, year_esperado, origen="cv",
                         publisher_esperado=None, numero_issue_muestra=None):
    if not resultados: return None, None, "ninguna", [], None

    query_limpia = limpiar_nombre_busqueda(query)
    exactos = [v for v in resultados if candidato_coincide_texto(v, query_limpia, origen, exacto=True)]
    candidatos = exactos if exactos else [v for v in resultados if candidato_coincide_texto(v, query_limpia, origen, exacto=False)]

    if not candidatos: return None, None, "ninguna", [], None

    if publisher_esperado:
        campo_pub = "publisher" if origen == "cv" else "publisher"
        con_editorial = [c for c in candidatos if publisher_esperado in (str((c.get(campo_pub) or {}).get("name", "")).lower())]
        if con_editorial: candidatos = con_editorial

    validos = []
    for c in candidatos:
        year = c.get("start_year") if origen == "cv" else c.get("year_began")
        year_int = int(year) if year and str(year).isdigit() else 9999
        validos.append((year_int, c))
    validos.sort(key=lambda x: x[0])
    candidatos = [v[1] for v in validos]

    if year_esperado:
        for c in candidatos:
            year = str(c.get("start_year") if origen == "cv" else c.get("year_began"))
            if year == str(year_esperado): return str(c.get("id")), c.get("name"), "alta", candidatos, "exacto_anio"

    if len(candidatos) == 1:
        return str(candidatos[0].get("id")), candidatos[0].get("name"), "alta", candidatos, "unico"

    if numero_volumen and str(numero_volumen).isdigit():
        idx = int(numero_volumen) - 1
        if 0 <= idx < len(candidatos): return str(candidatos[idx].get("id")), candidatos[idx].get("name"), "dudosa", candidatos, "indice_volumen"

    if total_comics > 1:
        mejor_c = None
        menor_diff = 9999
        margen = max(5, int(total_comics * 0.25))
        for c in candidatos:
            field = "count_of_issues" if origen == "cv" else "issue_count"
            count = int(c.get(field) or 0)
            diff = abs(count - total_comics)
            if diff < menor_diff and diff <= margen:
                menor_diff = diff
                mejor_c = c
        if mejor_c:
            if menor_diff <= 2:
                if issue_existe_en_candidato(str(mejor_c.get("id")), numero_issue_muestra, origen): confianza, metodo = "alta", "conteo_verificado"
                else: confianza, metodo = "dudosa", "conteo_sin_verificar"
            else: confianza, metodo = "dudosa", "conteo_aproximado"
            return str(mejor_c.get("id")), mejor_c.get("name"), confianza, candidatos, metodo

    return str(candidatos[0].get("id")), candidatos[0].get("name"), "dudosa", candidatos, "primero_lista"

def limpiar_ruido_nombre_serie(nombre_serie):
    n = re.sub(r"(?i)\bvol\.?\s*\d+", "", nombre_serie).strip()
    n = re.sub(r"\(\d{4}\)", "", n).strip()
    n = re.sub(r"\b\d+\s*-\s*\d+\b", "", n).strip()
    return n

def buscar_cv_id_en_comic_vine(nombre_serie, numero_volumen="1", total_comics=1, es_anual=False, es_special=False,
                                year_esperado=None, publisher_esperado=None, numero_issue_muestra=None):
    if not COMICVINE_API_KEY: return None, None, "ninguna", [], None
    nombre_puro = limpiar_ruido_nombre_serie(nombre_serie)
    nombre_puro = decodificar_leetspeak(nombre_puro)

    for variante in generar_variantes_busqueda(nombre_puro, numero_volumen):
        if es_anual: query = f"{variante} Annual"
        elif es_special: query = f"{variante} Special"
        else: query = variante

        query_url = quote(re.sub(r'[^a-zA-Z0-9]', ' ', query).strip())
        campos = "id,name,aliases,start_year,count_of_issues,publisher,image"
        url = (f"https://comicvine.gamespot.com/api/search/?api_key={COMICVINE_API_KEY}&format=json"
               f"&query={query_url}&resources=volume&field_list={campos}")
        data = peticion_api_segura(url)
        if data and data.get("results"):
            resultado = procesar_candidatos(data["results"], query, numero_volumen, total_comics, year_esperado,
                                             "cv", publisher_esperado, numero_issue_muestra)
            if resultado[0] is not None: return resultado
    return None, None, "ninguna", [], None

def buscar_mt_id_en_metron(nombre_serie, numero_volumen="1", total_comics=1, es_anual=False, es_special=False,
                            year_esperado=None, publisher_esperado=None, numero_issue_muestra=None):
    if not METRON_TOKEN: return None, None, "ninguna", [], None
    nombre_puro = limpiar_ruido_nombre_serie(nombre_serie)
    nombre_puro = decodificar_leetspeak(nombre_puro)

    for variante in generar_variantes_busqueda(nombre_puro, numero_volumen):
        query_url = quote(re.sub(r'[^a-zA-Z0-9]', ' ', variante).strip())
        url = f"https://metron.cloud/api/series/?name={query_url}"
        data = peticion_api_metron(url)
        if data and data.get("results"):
            resultado = procesar_candidatos(data["results"], variante, numero_volumen, total_comics, year_esperado,
                                             "mt", publisher_esperado, numero_issue_muestra)
            if resultado[0] is not None: return resultado
    return None, None, "ninguna", [], None

def obtener_datos_volumen(cv_id):
    mapa, mapa_ann, mapa_spc, vol_info, pub, tot = {}, {}, {}, {}, None, 0
    url = f"https://comicvine.gamespot.com/api/volume/4050-{cv_id}/?api_key={COMICVINE_API_KEY}&format=json&field_list=issues,publisher,name,count_of_issues"
    data = peticion_api_segura(url)
    if data and data.get("results"):
        res = data["results"]
        if isinstance(res, list): res = res[0] if res else {}
        vol_info["name"] = res.get("name")
        tot = res.get("count_of_issues") or 0
        if res.get("publisher"): pub = res.get("publisher").get("name")
        for issue in res.get("issues", []):
            num = str(issue.get("issue_number", "")).strip().lstrip("0") or "0"
            iss_id = issue.get("id")
            name_low = str(issue.get("name", "")).lower()
            url_low = str(issue.get("site_detail_url", "")).lower()
            if "annual" in name_low or "annual" in url_low: mapa_ann[num] = iss_id
            elif "special" in name_low or "special" in url_low: mapa_spc[num] = iss_id
            else: mapa[num] = iss_id
    return (mapa, mapa_ann, mapa_spc, vol_info, pub, tot)

def consultar_comic_vine_issue(issue_id):
    url = f"https://comicvine.gamespot.com/api/issue/4000-{issue_id}/?api_key={COMICVINE_API_KEY}&format=json"
    data = peticion_api_segura(url)
    if not data: return None
    res = data.get("results", {})
    if isinstance(res, dict): return res
    if isinstance(res, list) and len(res) > 0: return res[0]
    return {}

def obtener_datos_volumen_metron(mt_id):
    mapa, mapa_ann, mapa_spc, vol_info, pub, tot = {}, {}, {}, {}, None, 0
    url_series = f"https://metron.cloud/api/series/{mt_id}/"
    data_series = peticion_api_metron(url_series)
    if data_series:
        vol_info["name"] = data_series.get("name")
        tot = data_series.get("issue_count", 0)
        pub = data_series.get("publisher", {}).get("name")
        url_issues = f"https://metron.cloud/api/issue/?series_id={mt_id}"
        data_issues = peticion_api_metron(url_issues)
        if data_issues and data_issues.get("results"):
            for issue in data_issues["results"]:
                num = str(issue.get("number", "")).strip().lstrip("0") or "0"
                iss_id = issue.get("id")
                name_low = str(issue.get("issue_name", "")).lower() + " " + str(issue.get("name", "")).lower()
                if "annual" in name_low: mapa_ann[num] = iss_id
                elif "special" in name_low: mapa_spc[num] = iss_id
                else: mapa[num] = iss_id
    return (mapa, mapa_ann, mapa_spc, vol_info, pub, tot)

def consultar_metron_issue(issue_id):
    url = f"https://metron.cloud/api/issue/{issue_id}/"
    data = peticion_api_metron(url)
    if not data: return None
    cv_issue = {
        "issue_number": data.get("number"),
        "name": data.get("issue_name") or data.get("name"),
        "cover_date": data.get("cover_date"),
        "description": data.get("desc"),
        "site_detail_url": f"https://metron.cloud/issue/{data.get('id')}/",
        "image_url": data.get("image"),
        "person_credits": [],
        "character_credits": [],
        "story_arc_credits": [],
        "rated": data.get("rating", {}).get("name") if isinstance(data.get("rating"), dict) else data.get("rating"),
        "volume": {"name": data.get("series", {}).get("name") if isinstance(data.get("series"), dict) else data.get("series"),
                   "id": data.get("series", {}).get("id") if isinstance(data.get("series"), dict) else data.get("series")}
    }
    for c in data.get("credits", []): cv_issue["person_credits"].append({"name": c.get("creator"), "role": c.get("role", {}).get("name")})
    for c in data.get("characters", []): cv_issue["character_credits"].append({"name": c.get("name")})
    for arc in data.get("arcs", []): cv_issue["story_arc_credits"].append({"name": arc.get("name")})
    return cv_issue

def resolver_issue_id_y_datos(cv_id, mt_id, numero_issue, es_annual=False, es_special=False):
    if cv_id: mapa, mapa_ann, mapa_spc, vol_info, pub, tot = obtener_datos_volumen(cv_id)
    elif mt_id: mapa, mapa_ann, mapa_spc, vol_info, pub, tot = obtener_datos_volumen_metron(mt_id)
    else: return None, {}, None, 0
    num = str(numero_issue).lstrip("0") or "0"
    if es_annual: return mapa_ann.get(num), vol_info, pub, tot
    if es_special: return mapa_spc.get(num), vol_info, pub, tot
    return mapa.get(num), vol_info, pub, tot

def extraer_numero_issue_del_nombre(nombre_archivo):
    def limpiar_decimales(num_str):
        if "." in num_str:
            try:
                if float(num_str).is_integer(): return str(int(float(num_str)))
            except: pass
        return num_str

    if "annual" in nombre_archivo.lower() or "anual" in nombre_archivo.lower():
        m = re.search(r"(?:annual|anual)\D*(\d+)", nombre_archivo, re.I)
        return limpiar_decimales(m.group(1)) if m else "1"
    m = re.search(r"(?:#|n[º°]|issue\s*)\s*(\d+(?:\.\d+)?)", nombre_archivo, re.I)
    if m: return limpiar_decimales(m.group(1))
    
    nombre_limpio = os.path.splitext(nombre_archivo)[0]
    nombre_limpio = re.sub(r"[\(\[]\d{4}[\]\)]", "", nombre_limpio)
    nombre_limpio = re.sub(r"(?i)\b(?:vol\.?|volume|volumen|v)\s*\d+", "", nombre_limpio)
    nums = re.findall(r"(?<![a-zA-Z])(\d+(?:\.\d+)?)(?![a-zA-Z])", nombre_limpio)
    return limpiar_decimales(nums[-1]) if nums else "1"

def extraer_issue_id_explicito(nombre_archivo):
    m = re.search(r"\[issue-(\d+)\]", nombre_archivo, re.I)
    return m.group(1) if m else None

def limpiar_nombre_serie(nombre):
    return re.sub(r"\[cv-\d+\]|\[mt-\d+\]", "", nombre).strip()

def obtener_serie_y_volumen(carpeta_archivo, directorio_base):
    try: rel = os.path.relpath(carpeta_archivo, directorio_base)
    except: return os.path.basename(directorio_base), None
    partes = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not partes: return os.path.basename(directorio_base), None
    if os.path.basename(directorio_base).lower() in {"comics", "media", "servidor"}:
        return partes[0], partes[1] if len(partes) > 1 else None
    return os.path.basename(directorio_base), partes[0]

def extraer_numero_volumen(nombre_carpeta):
    """Solo extrae número de volumen si viene precedido explícitamente de Vol o Volume."""
    if not nombre_carpeta: return None
    limpio = limpiar_nombre_serie(nombre_carpeta)
    m = re.search(r"(?:vol|volume|volumen)\.?\s*(\d+)", limpio, re.I)
    return m.group(1) if m else None

def asignar_id_a_carpeta_si_falta(ruta_carpeta, serie_nombre, numero_volumen, directorio_base):
    cv_id, mt_id = obtener_ids_carpeta(ruta_carpeta, directorio_base)
    if cv_id or mt_id: return cv_id, mt_id

    nombre_actual = os.path.basename(ruta_carpeta)
    try: archivos_comic = sorted(f for f in os.listdir(ruta_carpeta) if f.lower().endswith((".cbz", ".cbr")))
    except Exception: archivos_comic = []
    total_archivos = len(archivos_comic)
    match_year = re.search(r"\((\d{4})\)", nombre_actual)
    year_carpeta = match_year.group(1) if match_year else None

    alias_cv, alias_mt, alias_nombre = buscar_alias_aprendido(serie_nombre)
    if alias_cv or alias_mt:
        tprint(f"  💡 Alias aprendido: '{serie_nombre}' -> '{alias_nombre}'")
        guardar_metadatos_carpeta(ruta_carpeta, directorio_base, alias_cv, alias_mt, alias_nombre or serie_nombre,
                                   numero_volumen, metodo="alias")
        return alias_cv, alias_mt

    publisher_esperado = detectar_editorial_por_ruta(ruta_carpeta, directorio_base)

    numero_issue_muestra = None
    archivo_para_caratula = None
    
    if archivos_comic:
        numero_issue_muestra = extraer_numero_issue_del_nombre(archivos_comic[-1])
        archivo_para_caratula = archivos_comic[0]
        for f in archivos_comic:
            num_f = extraer_numero_issue_del_nombre(f)
            if num_f in ["1", "01", "001", "1.0"]:
                archivo_para_caratula = f
                break

    aviso_editorial = f" | Editorial esperada: {publisher_esperado}" if publisher_esperado else ""
    tprint(f"🔍 Evaluando carpeta: '{serie_nombre}' (Vol. {numero_volumen}) | Archivos: {total_archivos}{aviso_editorial}...")

    api_id, nombre_oficial, confianza, candidatos_cv, metodo = buscar_cv_id_en_comic_vine(
        serie_nombre, numero_volumen, total_archivos, False, False, year_carpeta,
        publisher_esperado, numero_issue_muestra)
    fuente = "cv" if api_id else None
    candidatos_para_caratula = candidatos_cv
    origen_candidatos = "cv"

    if (not api_id or confianza != "alta") and METRON_TOKEN:
        tprint(f"  🔄 CV sin confianza alta. Probando en Metron (Plan B)…")
        api_id_mt, nombre_mt, confianza_mt, candidatos_mt, metodo_mt = buscar_mt_id_en_metron(
            serie_nombre, numero_volumen, total_archivos, False, False, year_carpeta,
            publisher_esperado, numero_issue_muestra)
        if api_id_mt and (confianza_mt == "alta" or not api_id):
            api_id, nombre_oficial, confianza, fuente, metodo = api_id_mt, nombre_mt, confianza_mt, "mt", metodo_mt
            candidatos_para_caratula, origen_candidatos = candidatos_mt, "mt"

    if api_id and confianza == "alta":
        cv_val = api_id if fuente == "cv" else None
        mt_val = api_id if fuente == "mt" else None
        guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_val, mt_val, nombre_oficial or serie_nombre,
                                   numero_volumen, metodo=metodo)
        guardar_alias_aprendido(serie_nombre, cv_val, mt_val, nombre_oficial or serie_nombre)
        return cv_val, mt_val

    if candidatos_para_caratula and archivo_para_caratula:
        ruta_representativa = os.path.join(ruta_carpeta, archivo_para_caratula)
        id_confirmado, nombre_confirmado = resolver_por_caratula(
            candidatos_para_caratula, ruta_representativa, origen_candidatos)
        if id_confirmado:
            cv_val = id_confirmado if origen_candidatos == "cv" else None
            mt_val = id_confirmado if origen_candidatos == "mt" else None
            guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_val, mt_val,
                                       nombre_confirmado or serie_nombre, numero_volumen, metodo="caratula")
            return cv_val, mt_val

    if api_id and confianza == "dudosa":
        tprint(f"  🟡 Match dudoso ('{nombre_oficial}') sin confirmar por carátula. No se cachea, va a revisión manual.")
    return None, None

def extraer_primera_imagen(ruta_comic):
    exts_img = (".jpg", ".jpeg", ".png", ".webp")
    ext = os.path.splitext(ruta_comic)[1].lower()
    try:
        if ext == ".cbz":
            with zipfile.ZipFile(ruta_comic, "r") as z:
                nombres = sorted(n for n in z.namelist()
                                  if n.lower().endswith(exts_img) and not os.path.basename(n).startswith(("._", ".")))
                if not nombres: return None
                data = z.read(nombres[0])
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(nombres[0])[1])
                tmp.write(data)
                tmp.close()
                return tmp.name
        elif ext == ".cbr":
            res = subprocess.run(["unrar", "lb", ruta_comic], capture_output=True, text=True)
            nombres = sorted(l.strip() for l in res.stdout.splitlines() if l.strip().lower().endswith(exts_img))
            if not nombres: return None
            primero = nombres[0]
            temp_dir = tempfile.mkdtemp()
            subprocess.run(["unrar", "e", "-inul", ruta_comic, primero, temp_dir + "/"], capture_output=True)
            extraidos = os.listdir(temp_dir)
            if not extraidos:
                shutil.rmtree(temp_dir)
                return None
            origen_path = os.path.join(temp_dir, extraidos[0])
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(extraidos[0])[1])
            shutil.copy(origen_path, tmp.name)
            shutil.rmtree(temp_dir)
            return tmp.name
    except Exception: return None
    return None

def calcular_phash_archivo(ruta_imagen):
    if not CARATULAS_DISPONIBLE or not ruta_imagen: return None
    try:
        with Image.open(ruta_imagen) as img: return imagehash.phash(img)
    except Exception: return None

def resolver_por_caratula(candidatos, ruta_comic_local, origen="cv"):
    if not CARATULAS_DISPONIBLE:
        tprint("  ⚠️  Escaneo de carátulas no disponible (falta 'pip install Pillow ImageHash').")
        return None, None
    if not candidatos: return None, None

    ruta_local = extraer_primera_imagen(ruta_comic_local)
    if not ruta_local: return None, None

    # Extraemos el texto de la portada con OCR (si está disponible)
    texto_ocr = extraer_texto_portada_ocr(ruta_local).lower() if OCR_DISPONIBLE else ""
    hash_local = calcular_phash_archivo(ruta_local)
    
    try: os.remove(ruta_local)
    except OSError: pass
    
    if hash_local is None: return None, None

    tprint(f"  🖼️  Escaneo de carátula: comparando contra {min(len(candidatos), 5)} candidato(s) ambiguo(s)…")
    mejor, menor_dist = None, 999
    for c in candidatos[:5]:
        img_info = c.get("image") or {}
        url_img = (img_info.get("small_url") or img_info.get("thumb_url")
                   or img_info.get("medium_url") or img_info.get("original_url"))
        if not url_img: continue
        ruta_temp = descargar_caratula_hq(url_img)
        if not ruta_temp: continue
        hash_c = calcular_phash_archivo(ruta_temp)
        try: os.remove(ruta_temp)
        except OSError: pass
        if hash_c is None: continue
        
        dist = hash_local - hash_c
        
        # 🚀 BONUS OCR: Si el nombre del candidato o el año están impresos en la portada, reducimos la distancia (mejoramos el match)
        if texto_ocr:
            nombre_c_limpio = limpiar_nombre_busqueda(c.get("name", "")).lower()
            year_c = str(c.get("start_year") or c.get("year_began") or "")
            if nombre_c_limpio and nombre_c_limpio in texto_ocr: dist -= 5
            if year_c and year_c in texto_ocr: dist -= 3

        if dist < menor_dist: menor_dist, mejor = dist, c

    UMBRAL_CONFIANZA = 12 # Umbral un poco más tolerante gracias al OCR
    if mejor and menor_dist <= UMBRAL_CONFIANZA:
        tprint(f"  ✅ Carátula/OCR confirma: '{mejor.get('name')}' (distancia calculada {menor_dist}).")
        return str(mejor.get("id")), mejor.get("name")

    tprint(f"  ⚠️  Ninguna carátula coincide con confianza (mejor distancia: {menor_dist}). Queda manual.")
    return None, None

def mapear_age_rating(rating_cv):
    r = str(rating_cv or "").strip().lower()
    if "mature" in r or "18" in r or "m" == r or "t+" in r: return "Mature 17+"
    if "everyone 10" in r or "e10" in r: return "Everyone 10+"
    if "everyone" in r or "e" == r or "all ages" in r: return "Everyone"
    return "Teen"

def convertir_real_cbr_a_cbz(ruta_archivo):
    ruta_base, ext = os.path.splitext(ruta_archivo)
    ruta_cbz = ruta_base + ".cbz"
    if ext.lower() == ".cbz" and os.path.exists(ruta_archivo):
        try:
            with zipfile.ZipFile(ruta_archivo, "r") as z:
                if z.testzip() is None: return ruta_archivo
        except Exception: pass

    if ext.lower() == ".cbr" and os.path.exists(ruta_cbz):
        try:
            with zipfile.ZipFile(ruta_cbz, "r") as z:
                if z.testzip() is None:
                    tprint(f"  ♻️  Ya existe un CBZ válido de una conversión anterior, reutilizando: {os.path.basename(ruta_cbz)}")
                    os.remove(ruta_archivo)
                    return ruta_cbz
        except Exception: pass

    temp_dir = os.path.join(os.path.dirname(ruta_archivo), "temp_conv_" + os.path.basename(ruta_archivo))
    try:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)
        tprint(f"  🔄 Convirtiendo a CBZ: {os.path.basename(ruta_archivo)}")

        res = subprocess.run(["unrar", "e", "-inul", ruta_archivo, temp_dir + "/"], capture_output=True)
        if res.returncode != 0 or not os.listdir(temp_dir):
            subprocess.run(["unzip", "-q", ruta_archivo, "-d", temp_dir + "/"], capture_output=True)

        archivos_extraidos = []
        for rt, _, fnames in os.walk(temp_dir):
            for fname in sorted(fnames): archivos_extraidos.append(os.path.join(rt, fname))

        if not archivos_extraidos:
            shutil.rmtree(temp_dir)
            return ruta_archivo

        with zipfile.ZipFile(ruta_cbz, "w", zipfile.ZIP_DEFLATED) as z_out:
            for ruta_img in archivos_extraidos:
                z_out.write(ruta_img, os.path.basename(ruta_img))

        shutil.rmtree(temp_dir)
        try:
            with zipfile.ZipFile(ruta_cbz, "r") as z_check:
                if z_check.testzip() is not None: raise ValueError("CBZ corrupto")
        except Exception:
            if os.path.exists(ruta_cbz): os.remove(ruta_cbz)
            return ruta_archivo
        os.remove(ruta_archivo)
        return ruta_cbz
    except Exception:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
    return ruta_archivo

def traducir_texto(texto):
    if not texto or len(texto.strip()) < 5: return texto
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=es&dt=t&q=" + quote(texto)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            return "".join([s[0] for s in data[0] if s[0]])
    except Exception: return texto

def inyectar_metadatos_oficiales(ruta_cbz, info_issue, serie_nombre, numero_volumen, numero_issue_forzado, publisher_volumen, total_issues, is_annual, is_special, ruta_caratula=None):
    try:
        root = None
        try:
            with zipfile.ZipFile(ruta_cbz, "r") as z:
                if "ComicInfo.xml" in z.namelist():
                    root = ET.fromstring(z.read("ComicInfo.xml"))
        except Exception: pass
        
        if root is None: root = ET.Element("ComicInfo")
        def set_tag(tag, val):
            if val is not None and str(val).strip():
                elem = root.find(tag)
                if elem is None: elem = ET.SubElement(root, tag)
                elem.text = str(val).strip()
                
        set_tag("Series", serie_nombre)
        if not is_annual and numero_volumen: set_tag("Volume", str(numero_volumen))
        set_tag("Number", str(info_issue.get("issue_number") or numero_issue_forzado or "1").strip())
        if total_issues and int(total_issues) > 0: set_tag("Count", str(total_issues))
        
        # 🚀 DETECCIÓN DE TOMOS Y FORMATOS RECOPILATORIOS
        nombre_archivo = os.path.basename(ruta_cbz).lower()
        m_tomo = re.search(r"(?i)(integral|todo en 1|omnibus|tpb)", nombre_archivo)
        
        if is_annual: set_tag("Format", "Annual")
        elif is_special: set_tag("Format", "Special")
        elif m_tomo: set_tag("Format", "Trade Paperback") # Kavita lo tratará como tomo
        
        title = info_issue.get("name")
        if not title or str(title).lower().startswith("issue #"):
            title = re.sub(r"\[issue-\d+\]|\[cv-\d+\]|\[mt-\d+\]", "", os.path.basename(ruta_cbz).replace(".cbz", ""), flags=re.I).strip()
        set_tag("Title", title)
        set_tag("Publisher", publisher_volumen or "Marvel")
        set_tag("AgeRating", mapear_age_rating(info_issue.get("rated")))
        set_tag("LanguageISO", "es")
        set_tag("Web", info_issue.get("site_detail_url", ""))
        
        year = None
        if info_issue.get("cover_date"):
            parts = info_issue.get("cover_date").split("-")
            year = parts[0]
            set_tag("Year", year)
            if len(parts) > 1: set_tag("Month", parts[1])
            if len(parts) > 2: set_tag("Day", parts[2])
            
        summary = info_issue.get("description")
        traducido = False
        if summary:
            summary_clean = re.sub("<.*?>", "", summary)
            summary_trans = traducir_texto(summary_clean)
            if summary_trans != summary_clean: traducido = True
            set_tag("Summary", summary_trans)
            
        writers, pencillers, inkers, colorists, letterers, editors = [], [], [], [], [], []
        for p in info_issue.get("person_credits", []):
            name = p.get("name")
            role = str(p.get("role") or "").lower()
            if not name: continue
            if "writer" in role: writers.append(name)
            elif "penciler" in role or "artist" in role: pencillers.append(name)
            elif "inker" in role: inkers.append(name)
            elif "color" in role: colorists.append(name)
            elif "letter" in role: letterers.append(name)
            elif "edit" in role: editors.append(name)
            
        set_tag("Writer", ", ".join(writers))
        set_tag("Penciller", ", ".join(pencillers))
        set_tag("Inker", ", ".join(inkers))
        set_tag("Colorist", ", ".join(colorists))
        
        chars = [c.get("name") for c in info_issue.get("character_credits", []) if c.get("name")][:5]
        
        # 🚀 SINCRONIZACIÓN DE EVENTOS Y ÓRDENES DE LECTURA (Crossovers)
        arcs = [arc.get("name") for arc in info_issue.get("story_arc_credits", []) if arc.get("name")]
        if arcs:
            set_tag("StoryArc", arcs[0])
            set_tag("AlternateSeries", arcs[0]) # Kavita creará una colección de lectura con el nombre del evento
            
        tags = list(set(chars + arcs + [serie_nombre]))
        set_tag("Characters", ", ".join(chars))
        set_tag("Tags", ", ".join(tags))
        if "genre" in info_issue: set_tag("Genre", info_issue["genre"])
        
        xml_content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        temp_path = ruta_cbz + ".tmp_xml"
        seen_files = set()
        
        conf_caratulas = CONFIG_GENERAL.get("caratulas", {})
        guardar_como = conf_caratulas.get("guardar_como", "cbz_interno")

        with zipfile.ZipFile(ruta_cbz, "r") as z_in, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as z_out:
            if ruta_caratula and guardar_como == "cbz_interno" and os.path.exists(ruta_caratula):
                z_out.write(ruta_caratula, "0000_Cover_HQ.jpg")
                seen_files.add("0000_Cover_HQ.jpg")
                
            for item in z_in.infolist():
                if item.filename == "ComicInfo.xml" or ".DS_Store" in item.filename or item.filename.startswith("._"): continue
                if item.filename not in seen_files:
                    z_out.writestr(item, z_in.read(item.filename))
                    seen_files.add(item.filename)
            z_out.writestr("ComicInfo.xml", xml_content)
            
        os.replace(temp_path, ruta_cbz)

        if ruta_caratula and guardar_como == "carpeta_externa" and os.path.exists(ruta_caratula):
            ruta_cover_externo = os.path.join(os.path.dirname(ruta_cbz), "cover.jpg")
            if not os.path.exists(ruta_cover_externo): shutil.copy(ruta_caratula, ruta_cover_externo)

        return True, traducido, year
    except Exception:
        tmp = ruta_cbz + ".tmp_xml"
        if os.path.exists(tmp): os.remove(tmp)
        return False, False, None

def renombrar_archivo_nativo(ruta_cbz, serie_nombre, numero_volumen, numero_issue, year, is_annual, is_special):
    carpeta = os.path.dirname(ruta_cbz)
    parts = str(numero_issue).split(".")
    num_val = parts[0].zfill(3) + (f".{parts[1]}" if len(parts) > 1 else "")
    serie_limpia = re.sub(r"(?i)\bvol\.?\s*\d+", "", serie_nombre).strip()

    if is_annual: centro = f"Annual #{num_val}"
    elif is_special: centro = f"Special #{num_val}"
    else: centro = f"Vol {numero_volumen} #{num_val}" if numero_volumen else f"Vol 1 #{num_val}"

    nombre_base = f"{serie_limpia} {centro}"
    if year: nombre_base += f" ({year})"
    nombre_base += ".cbz"
    
    nuevo_nombre = re.sub(r'[\\/:*?"<>|]', "-", nombre_base).strip()
    nueva_ruta = os.path.join(carpeta, nuevo_nombre)
    
    if os.path.exists(nueva_ruta) and os.path.abspath(ruta_cbz) != os.path.abspath(nueva_ruta):
        nueva_ruta = f"{os.path.splitext(nueva_ruta)[0]}_v2.cbz"
    
    if ruta_cbz != nueva_ruta:
        os.rename(ruta_cbz, nueva_ruta)
        return nueva_ruta, True
    return ruta_cbz, False

# ==========================================
# MÓDULO DE TAREA INDIVIDUAL (MULTIHILO)
# ==========================================
def procesar_un_archivo(root, file, directorio_base, cbz_files_list, cv_id_carp, mt_id_carp, imprimir_consola=True):
    r_orig = os.path.join(root, file)
    fl = file.lower()
    tprint(f"🎯 Procesando: {file}", imprimir_consola)
    temp_cover_path = None
    
    try:
        nombre_carpeta_actual = os.path.basename(root)
        sb, v_txt = obtener_serie_y_volumen(root, directorio_base)
        sb_limpia = limpiar_nombre_serie(sb)
        sb_pura = re.sub(r"(?i)\bvol\.?\s*\d+", "", sb_limpia).strip()
        v_num = extraer_numero_volumen(nombre_carpeta_actual)
        
        iss_id_exp = extraer_issue_id_explicito(file)
        if not iss_id_exp:
            ruta_relativa = os.path.relpath(r_orig, directorio_base)
            with DB_LOCK:
                cursor = DB_CONN.cursor()
                cursor.execute("SELECT cv_id FROM archivos WHERE ruta_relativa = ?", (ruta_relativa,))
                row = cursor.fetchone()
            if row and row[0].isdigit(): iss_id_exp = row[0]

        is_ann = "annual" in fl or "anual" in fl
        is_spc = "special" in fl or "one-shot" in fl
        n_iss = extraer_numero_issue_del_nombre(re.sub(r"\[issue-\d+\]", "", file, flags=re.I))
        iss_id, info_iss, pub_v, tot_iss = None, {}, None, 0
        fuente = "cv"
        
        if iss_id_exp:
            iss_id = iss_id_exp
            info = consultar_comic_vine_issue(iss_id)
            if not info and METRON_TOKEN:
                info = consultar_metron_issue(iss_id)
                fuente = "mt"
            if info and info.get("volume", {}).get("id"):
                vol_id = info["volume"]["id"]
                _, _, _, _, pub_v, tot_iss = obtener_datos_volumen(vol_id)
                info_iss = info
                
                # 🚀 PROPAGACIÓN INVERSA (De Archivo a Carpeta)
                if not cv_id_carp and fuente == "cv":
                    guardar_metadatos_carpeta(root, directorio_base, str(vol_id), mt_id_carp, info["volume"].get("name", sb_pura), v_num, metodo="propagacion_inversa")
                    cv_id_carp = str(vol_id)
                    tprint(f"  🧬 [{file}] [Propagación Inversa] Carpeta identificada automáticamente gracias al ID de este archivo.", imprimir_consola)
                elif not mt_id_carp and fuente == "mt":
                    guardar_metadatos_carpeta(root, directorio_base, cv_id_carp, str(vol_id), info["volume"].get("name", sb_pura), v_num, metodo="propagacion_inversa")
                    mt_id_carp = str(vol_id)
                    tprint(f"  🧬 [{file}] [Propagación Inversa] Carpeta identificada automáticamente gracias al ID de este archivo.", imprimir_consola)

        else:
            iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(cv_id_carp, mt_id_carp, n_iss, is_ann, is_spc)
            fuente = "cv" if cv_id_carp else "mt"

            if (cv_id_carp or mt_id_carp) and not (is_ann or is_spc):
                registrar_intento_resolucion(root, directorio_base, exito=bool(iss_id))

            if not iss_id and cv_id_carp and METRON_TOKEN:
                mt_fallback = mt_id_carp
                if not mt_fallback: mt_fallback, _, _, _, _ = buscar_mt_id_en_metron(sb_pura, v_num, 1, is_ann, is_spc)
                if mt_fallback:
                    iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(None, mt_fallback, n_iss, is_ann, is_spc)
                    if iss_id:
                        fuente = "mt"
                        if not mt_id_carp: guardar_metadatos_carpeta(root, directorio_base, cv_id_carp, mt_fallback, sb_pura, v_num, metodo="propagacion_mt")
            
            if not iss_id and (is_ann or is_spc):
                v_ext_id_cv, _, _, _, _ = buscar_cv_id_en_comic_vine(sb_pura, "", 1, is_ann, is_spc)
                if v_ext_id_cv:
                    iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(v_ext_id_cv, None, n_iss, is_ann, is_spc)
                    fuente = "cv"
                elif METRON_TOKEN:
                    v_ext_id_mt, _, _, _, _ = buscar_mt_id_en_metron(sb_pura, "", 1, is_ann, is_spc)
                    if v_ext_id_mt:
                        iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(None, v_ext_id_mt, n_iss, is_ann, is_spc)
                        fuente = "mt"
        
        nombre_oficial_serie = sb_pura
        url_cover_hq = None
        
        if iss_id:
            if fuente == "cv":
                info_iss = consultar_comic_vine_issue(iss_id) or {}
                if info_iss.get("volume") and info_iss["volume"].get("name"): nombre_oficial_serie = info_iss["volume"]["name"]
                url_cover_hq = info_iss.get("image", {}).get("original_url")
            else:
                info_iss = consultar_metron_issue(iss_id) or {}
                if info_iss.get("volume") and info_iss["volume"].get("name"): nombre_oficial_serie = info_iss["volume"]["name"]
                url_cover_hq = info_iss.get("image_url")
        elif cv_id_carp or mt_id_carp:
            iss_id = "HEREDADO"
            fuente = "herencia"
            info_iss = {
                "issue_number": n_iss,
                "name": f"Issue #{n_iss}",
                "description": "Issue huérfano en la API. Metadatos básicos propagados desde el volumen padre."
            }
            if 'info_iss_tmp' in locals() and info_iss_tmp and info_iss_tmp.get("name"):
                nombre_oficial_serie = info_iss_tmp.get("name")
            tprint(f"  🧬 [{file}] Número huérfano en API. Propagando identidad de la carpeta '{nombre_oficial_serie}'...", imprimir_consola)

        r_orig = convertir_real_cbr_a_cbz(r_orig)

        if iss_id and info_iss:
            conf_caratulas = CONFIG_GENERAL.get("caratulas", {})
            if conf_caratulas.get("descargar_caratulas_hq") and url_cover_hq:
                temp_cover_path = descargar_caratula_hq(url_cover_hq)

            ok, trad, year = inyectar_metadatos_oficiales(r_orig, info_iss, nombre_oficial_serie, v_num, n_iss, pub_v, tot_iss, is_ann, is_spc, temp_cover_path)
            if ok:
                if trad: tprint(f"  🇪🇸 [{file}] Summary traducido al español.", imprimir_consola)
                r_orig, renombrado = renombrar_archivo_nativo(r_orig, nombre_oficial_serie, v_num, n_iss, year, is_ann, is_spc)
                if renombrado: tprint(f"  ✏️  [{file}] → {os.path.basename(r_orig)}", imprimir_consola)
                tprint(f"  ✅ [{file}] Metadatos oficiales ({fuente.upper()}) inyectados. "
                       f"[issue_id={iss_id} | Number={info_iss.get('issue_number')} | n_iss={n_iss}]\n", imprimir_consola)
                
                ruta_relativa_final = os.path.relpath(r_orig, directorio_base)
                with DB_LOCK:
                    cursor = DB_CONN.cursor()
                    cursor.execute("REPLACE INTO archivos (ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)",
                                   (ruta_relativa_final, iss_id_exp or iss_id, os.path.getmtime(r_orig)))
                    DB_CONN.commit()
                return (root, nombre_oficial_serie, v_num)
            else: tprint(f"  ❌ [{file}] Fallo al escribir XML.\n", imprimir_consola)
        else:
            tprint(f"  🟡 [{file}] Sin ID exacto de issue. Pendiente de revisión manual.\n", imprimir_consola)
            
    except Exception as e:
        tprint(f"❌ Error crítico procesando {file}: {e}\n", imprimir_consola)
    finally:
        if temp_cover_path and os.path.exists(temp_cover_path):
            try: os.remove(temp_cover_path)
            except: pass
            
    return None

def ejecutar_motor_procesamiento(directorio):
    carpetas_a_renombrar_al_final = {}
    directorio = os.path.abspath(directorio)
    iniciar_db(directorio)
    
    tprint(f"\n🚀 Iniciando escaneo profundo en: {directorio}\n")
    carpetas_sin_id = []
    
    for root, dirs, files in os.walk(directorio, topdown=False):
        cbz_files = [f for f in files if f.lower().endswith((".cbz", ".cbr"))]
        if not cbz_files: continue
        
        root_name = os.path.basename(root).lower()
        if "annual" in root_name or "special" in root_name: continue
        
        cv_id, mt_id = obtener_ids_carpeta(root, directorio)
        sb, _ = obtener_serie_y_volumen(root, directorio)
        sb_limpio = limpiar_nombre_serie(sb)
        nombre_actual = os.path.basename(root)
        
        if cv_id or mt_id:
            if not os.path.exists(os.path.join(root, ".comic_id.json")):
                guardar_metadatos_carpeta(root, directorio, cv_id, mt_id, sb_limpio,
                                           extraer_numero_volumen(nombre_actual), metodo="tag_legacy")
        else:
            res_cv, res_mt = asignar_id_a_carpeta_si_falta(root, sb_limpio, extraer_numero_volumen(nombre_actual), directorio)
            if not res_cv and not res_mt:
                carpetas_sin_id.append((root, sb_limpio, extraer_numero_volumen(nombre_actual)))

    tareas = []
    for root, _, files in os.walk(directorio):
        cbz_files = [f for f in files if f.lower().endswith((".cbz", ".cbr"))]
        if not cbz_files: continue
        
        cv_id_carp, mt_id_carp = obtener_ids_carpeta(root, directorio)
        
        for file in sorted(cbz_files):
            fl = file.lower()
            skip_patterns = ["traducido", "spanish", "crg", "custom", ".tmp"]
            if any(p in fl for p in skip_patterns): continue
            
            r_orig = os.path.join(root, file)
            mtime_act = os.path.getmtime(r_orig)
            ruta_relativa = os.path.relpath(r_orig, directorio)
            
            with DB_LOCK:
                cursor = DB_CONN.cursor()
                cursor.execute("SELECT mtime FROM archivos WHERE ruta_relativa = ?", (ruta_relativa,))
                row = cursor.fetchone()
            if row and abs(row[0] - mtime_act) < 5: continue
                
            tareas.append((root, file, directorio, cbz_files, cv_id_carp, mt_id_carp))

    if tareas:
        conf_rendimiento = CONFIG_GENERAL.get("rendimiento", {})
        usar_multihilo = conf_rendimiento.get("modo_escaneo_multiple", False)
        max_hilos = conf_rendimiento.get("hilos_simultaneos", 1)
        
        if usar_multihilo and max_hilos > 1:
            if TQDM_DISPONIBLE:
                tprint(f"\n⚡ Activando MODO MULTIHILO ({max_hilos} núcleos) con barra de progreso...")
                tprint("📝 Los detalles detallados del proceso se están guardando en 'comicscrapper.log'")
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_hilos) as executor:
                    # Pasamos False para que no ensucie la barra de progreso en la consola
                    futuros = {executor.submit(procesar_un_archivo, *t, False): t for t in tareas}
                    for futuro in tqdm(concurrent.futures.as_completed(futuros), total=len(tareas), desc="Procesando Cómics", unit="archivo", dynamic_ncols=True):
                        res = futuro.result()
                        if res:
                            root_r, nombre_r, v_num_r = res
                            carpetas_a_renombrar_al_final[root_r] = (nombre_r, v_num_r)
            else:
                tprint(f"\n⚡ Activando MODO MULTIHILO ({max_hilos} núcleos simultáneos)...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_hilos) as executor:
                    futuros = [executor.submit(procesar_un_archivo, *t, True) for t in tareas]
                    for futuro in concurrent.futures.as_completed(futuros):
                        res = futuro.result()
                        if res:
                            root_r, nombre_r, v_num_r = res
                            carpetas_a_renombrar_al_final[root_r] = (nombre_r, v_num_r)
        else:
            for t in tareas:
                res = procesar_un_archivo(*t, True) # Modo tradicional hilo único
                if res:
                    root_r, nombre_r, v_num_r = res
                    carpetas_a_renombrar_al_final[root_r] = (nombre_r, v_num_r)

    if carpetas_a_renombrar_al_final:
        tprint("\n📁 Optimizando nombres de carpetas según metadatos oficiales...")
        padres_ya_renombrados = {}

        for old_root, (true_name, vol) in carpetas_a_renombrar_al_final.items():
            root_name = os.path.basename(old_root).lower()
            if "annual" in root_name or "special" in root_name: continue

            titulo_limpio = re.sub(r'[\\/:*?"<>|]', '-', true_name).strip()
            padre_original = os.path.dirname(old_root)
            es_dos_niveles = os.path.normpath(padre_original) != os.path.normpath(directorio)

            padre_actual = padre_original
            if es_dos_niveles:
                if padre_original in padres_ya_renombrados:
                    padre_actual = padres_ya_renombrados[padre_original]
                elif os.path.basename(padre_original) != titulo_limpio:
                    nuevo_padre = os.path.join(os.path.dirname(padre_original), titulo_limpio)
                    if not os.path.exists(nuevo_padre):
                        try:
                            os.rename(padre_original, nuevo_padre)
                            tprint(f"  ✅ Carpeta madre corregida: '{titulo_limpio}'")
                            padre_actual = nuevo_padre
                        except OSError: padre_actual = padre_original
                    padres_ya_renombrados[padre_original] = padre_actual

            old_root_actual = os.path.join(padre_actual, os.path.basename(old_root))

            if es_dos_niveles and vol: nuevo_nombre_hijo = f"Vol {vol}"
            elif es_dos_niveles: nuevo_nombre_hijo = os.path.basename(old_root_actual)
            else: nuevo_nombre_hijo = titulo_limpio

            nuevo_root = os.path.join(padre_actual, nuevo_nombre_hijo)

            if old_root_actual != nuevo_root and not os.path.exists(nuevo_root) and os.path.exists(old_root_actual):
                try:
                    os.rename(old_root_actual, nuevo_root)
                    tprint(f"  ✅ Carpeta corregida: '{os.path.relpath(nuevo_root, directorio)}'")
                except OSError: nuevo_root = old_root_actual
            else: nuevo_root = old_root_actual if os.path.exists(old_root_actual) else nuevo_root

            if os.path.normpath(nuevo_root) != os.path.normpath(old_root):
                old_rel = os.path.relpath(old_root, directorio)
                new_rel = os.path.relpath(nuevo_root, directorio)
                with DB_LOCK:
                    cursor = DB_CONN.cursor()
                    cursor.execute("UPDATE OR REPLACE series SET ruta_carpeta = ?, nombre_oficial = ? WHERE ruta_carpeta = ?",
                                   (new_rel, true_name, old_rel))
                    cursor.execute("SELECT ruta_relativa FROM archivos WHERE ruta_relativa LIKE ?", (old_rel + "%",))
                    for row in cursor.fetchall():
                        vieja_ruta = row[0]
                        if vieja_ruta == old_rel or vieja_ruta.startswith(old_rel + os.sep):
                            nueva_ruta_archivo = new_rel + vieja_ruta[len(old_rel):]
                            cursor.execute("UPDATE OR REPLACE archivos SET ruta_relativa = ? WHERE ruta_relativa = ?",
                                           (nueva_ruta_archivo, vieja_ruta))
                    DB_CONN.commit()

    # 🚀 MANTENIMIENTO AUTOMÁTICO DE BASE DE DATOS
    tprint("\n🗄️  Realizando mantenimiento (VACUUM) de la base de datos...")
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        cursor.execute("VACUUM")
        DB_CONN.commit()

    return carpetas_sin_id

def escanear_solo_pendientes(directorio):
    tprint(f"\n🔍 Revisando elementos pendientes sin hacer llamadas a internet (modo rápido)...")
    iniciar_db(directorio)
    cursor = DB_CONN.cursor()
    carpetas_pendientes, archivos_pendientes = [], []

    for root, dirs, files in os.walk(directorio):
        cbz_files = [f for f in files if f.lower().endswith((".cbz", ".cbr"))]
        if not cbz_files: continue
        
        nc = os.path.basename(root)
        is_annual_folder = "annual" in nc.lower() or "special" in nc.lower()
        cv_id_carp, mt_id_carp = obtener_ids_carpeta(root, directorio)

        if not cv_id_carp and not mt_id_carp and not is_annual_folder:
            sb, _ = obtener_serie_y_volumen(root, directorio)
            carpetas_pendientes.append((root, limpiar_nombre_serie(sb), extraer_numero_volumen(nc)))

        for f in sorted(cbz_files):
            fl = f.lower()
            if any(p in fl for p in ["traducido", "spanish", "crg", "custom", ".tmp"]): continue
            
            ruta_relativa = os.path.relpath(os.path.join(root, f), directorio)
            cursor.execute("SELECT cv_id FROM archivos WHERE ruta_relativa = ?", (ruta_relativa,))
            if not cursor.fetchone():
                archivos_pendientes.append(os.path.join(root, f))

    return carpetas_pendientes, archivos_pendientes

def lanzar_asistente_manual(carpetas_pendientes, archivos_pendientes, directorio_base):
    try:
        if not carpetas_pendientes and not archivos_pendientes: return False
        cambios_carpetas = False
        cambios_archivos = False

        if carpetas_pendientes:
            print("\n" + "=" * 50)
            print(f"🤔 Hay {len(carpetas_pendientes)} serie(s) pendiente(s) de identificar.")
            if input("¿Deseas identificar series manualmente ahora? (S/N): ").strip().lower() == "s":
                for r_carp, s_base, n_vol in carpetas_pendientes:
                    print("\n" + "-" * 50)
                    print(f"📁 CARPETA: {r_carp}\n   📋 Serie detectada: '{s_base}' (Vol. {n_vol})\n" + "-" * 50)
                    print("  [1] 🌐 Buscar en Comic Vine\n  [2] 🌐 Buscar en Metron\n  [3] ✍️  Pegar ID de Comic Vine\n  [4] ✍️  Pegar ID de Metron\n  [5] ⏭️  Saltar esta carpeta\n  [6] 🛑 Cerrar la identificación")
                    while True:
                        accion = input("\nElige una opción (1-6) ➡️ ").strip()
                        if accion in ["1", "2", "3", "4", "5", "6"]: break
                        print("  ⚠️ Por favor, elige del 1 al 6.")
                    if accion == "6": break
                    if accion == "5": continue
                    
                    cv_id, mt_id, nombre_oficial = None, None, s_base
                    if accion == "3": cv_id = input("   ✍️ Pega el ID de Comic Vine: ").strip()
                    elif accion == "4": mt_id = input("   ✍️ Pega el ID de Metron: ").strip()
                    elif accion == "1":
                        q = input(f"   🌐 Búsqueda CV ['{s_base} vol {n_vol}']: ").strip() or f"{s_base} vol {n_vol}"
                        res = peticion_api_segura(f"https://comicvine.gamespot.com/api/search/?api_key={COMICVINE_API_KEY}&format=json&query={quote(q)}&resources=volume")
                        if res and res.get("results"):
                            for i, v in enumerate(res["results"][:5]):
                                print(f"     [{i+1}] {v.get('name')} ({v.get('start_year')}) [ID: {v.get('id')}]")
                            sel = input("\n   Elige número de la lista (0 para omitir): ").strip()
                            if sel.isdigit() and 1 <= int(sel) <= 5: cv_id = str(res["results"][int(sel)-1].get("id"))
                        else: print("   ⚠️ Sin resultados.")
                    elif accion == "2" and METRON_TOKEN:
                        q = input(f"   🌐 Búsqueda Metron ['{s_base} vol {n_vol}']: ").strip() or f"{s_base} vol {n_vol}"
                        res = peticion_api_metron(f"https://metron.cloud/api/series/?name={quote(q)}")
                        if res and res.get("results"):
                            for i, v in enumerate(res["results"][:5]):
                                print(f"     [{i+1}] {v.get('name')} ({v.get('year_began')}) [ID: {v.get('id')}]")
                            sel = input("\n   Elige número de la lista (0 para omitir): ").strip()
                            if sel.isdigit() and 1 <= int(sel) <= 5: mt_id = str(res["results"][int(sel)-1].get("id"))
                        else: print("   ⚠️ Sin resultados en Metron.")
                        
                    if (cv_id and cv_id.isdigit()) or (mt_id and mt_id.isdigit()):
                        guardar_metadatos_carpeta(r_carp, directorio_base, cv_id, mt_id, nombre_oficial, n_vol, metodo="manual")
                        guardar_alias_aprendido(s_base, cv_id, mt_id, nombre_oficial or s_base)
                        cambios_carpetas = True

        if cambios_carpetas:
            print("\n" + "=" * 50)
            print("🔄 Has asignado nuevos IDs a las carpetas.")
            if input("¿Deseas que el motor escanee sus cómics automáticamente AHORA MISMO? (S/N): ").strip().lower() == "s":
                return True

        if archivos_pendientes:
            print("\n" + "=" * 40)
            print(f"🤔 Hay {len(archivos_pendientes)} CÓMICS INDIVIDUALES sin ID asignado.")
            if input("¿Deseas asignar IDs de ISSUE y propagarlos? (S/N): ").strip().lower() == "s":
                carpetas_saltadas = set()
                for r_archivo in archivos_pendientes:
                    carpeta_actual = os.path.dirname(r_archivo)
                    if carpeta_actual in carpetas_saltadas: continue
                    print("\n" + "-" * 50)
                    print(f"📄 ARCHIVO: {os.path.basename(r_archivo)}\n  [1] ✍️  Pegar ID de CV\n  [2] ✍️  Pegar ID de Metron\n  [3] ⏭️  Saltar\n  [4] 📂 Saltar carpeta entera\n  [5] 🛑 Cerrar")
                    while True:
                        accion = input("\nElige una opción (1-5) ➡️ ").strip()
                        if accion in ["1", "2", "3", "4", "5"]: break
                    if accion == "5": break
                    if accion == "4": carpetas_saltadas.add(carpeta_actual); continue
                    if accion == "3": continue
                    
                    issue_id = input("   ✍️ Pega el ID del ISSUE: ").strip()
                    if issue_id.isdigit():
                        cv_vol_id, mt_vol_id, nombre_serie_oficial = None, None, None
                        if accion == "1":
                            info_cv = consultar_comic_vine_issue(issue_id)
                            if info_cv and info_cv.get("volume", {}).get("id"):
                                cv_vol_id = str(info_cv["volume"]["id"])
                                nombre_serie_oficial = info_cv["volume"].get("name")
                        elif accion == "2" and METRON_TOKEN:
                            info_mt = consultar_metron_issue(issue_id)
                            if info_mt and info_mt.get("volume"):
                                mt_vol_id = str(info_mt["volume"].get("id") if isinstance(info_mt["volume"], dict) else info_mt["volume"])
                                nombre_serie_oficial = info_mt["volume"].get("name") if isinstance(info_mt["volume"], dict) else "Unknown"
                        
                        if cv_vol_id or mt_vol_id:
                            ruta_relativa = os.path.relpath(r_archivo, directorio_base)
                            with DB_LOCK:
                                cursor = DB_CONN.cursor()
                                cursor.execute("REPLACE INTO archivos (ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)", (ruta_relativa, issue_id, 0))
                                is_ann_spc = "annual" in r_archivo.lower() or "special" in r_archivo.lower()
                                if not is_ann_spc:
                                    n_vol = extraer_numero_volumen(os.path.basename(carpeta_actual))
                                    guardar_metadatos_carpeta(carpeta_actual, directorio_base, cv_vol_id, mt_vol_id,
                                                               nombre_serie_oficial or os.path.basename(carpeta_actual), n_vol, metodo="manual")
                                    guardar_alias_aprendido(os.path.basename(carpeta_actual), cv_vol_id, mt_vol_id,
                                                             nombre_serie_oficial or os.path.basename(carpeta_actual))
                                DB_CONN.commit()
                            cambios_archivos = True
                            
        if cambios_carpetas or cambios_archivos: 
            print("\n🔄 Hay cambios en cola. Vuelve a ejecutar 'python3 comicscrapper.py start'.")
        return False
    except KeyboardInterrupt: sys.exit(0)

def resetear_carpeta(objetivo, dir_base_elegido):
    ruta_relativa = os.path.relpath(objetivo, dir_base_elegido)
    with DB_LOCK:
        cursor = DB_CONN.cursor()
        json_path = os.path.join(objetivo, ".comic_id.json")
        if os.path.exists(json_path):
            try: os.remove(json_path)
            except OSError: pass
        cursor.execute("DELETE FROM series WHERE ruta_carpeta = ?", (ruta_relativa,))
        cursor.execute("DELETE FROM archivos WHERE ruta_relativa LIKE ?", (ruta_relativa + "%",))
        DB_CONN.commit()

def comando_doctor(config):
    metodos_debiles = ("primero_lista", "indice_volumen", "conteo_aproximado", "conteo_sin_verificar")
    encontrados_total = 0

    for ruta_base in config.get("rutas_comics", []):
        if not os.path.exists(ruta_base): continue
        iniciar_db(ruta_base)
        cursor = DB_CONN.cursor()
        placeholders = ",".join("?" * len(metodos_debiles))
        cursor.execute(f"""SELECT ruta_carpeta, cv_id, mt_id, nombre_oficial, metodo,
                                   sospechosa, pendiente_confirmar, total_intentos, total_fallos, fecha_asignacion
                            FROM series
                            WHERE sospechosa = 1 OR pendiente_confirmar = 1 OR metodo IN ({placeholders})
                            ORDER BY sospechosa DESC, pendiente_confirmar DESC, fecha_asignacion ASC""",
                       metodos_debiles)
        filas = cursor.fetchall()
        if not filas: continue

        print(f"\n🩺 {ruta_base}: {len(filas)} carpeta(s) con menos garantías en la caché.\n")
        for ruta_rel, cv_id, mt_id, nombre, metodo, sosp, pend, intentos, fallos, fecha in filas:
            encontrados_total += 1
            motivos = []
            if sosp: motivos.append(f"tasa de fallo real {fallos}/{intentos}")
            if pend: motivos.append("identificada solo por carátula, sin confirmar")
            if metodo in metodos_debiles: motivos.append(f"método débil: {metodo}")
            fecha_str = time.strftime("%Y-%m-%d", time.localtime(fecha)) if fecha else "?"
            print(f"  📁 {ruta_rel}")
            print(f"     '{nombre}'  (cv={cv_id or '-'} mt={mt_id or '-'})  [{fecha_str}]  -> {', '.join(motivos)}")

            resp = input("     (R)esetear / (S)altar / (Q) salir del doctor -> ").strip().lower()
            if resp == "q":
                print("\n👋 Saliendo de 'doctor'.")
                return
            if resp == "r":
                objetivo = os.path.join(ruta_base, ruta_rel)
                resetear_carpeta(objetivo, ruta_base)
                print("     ✅ Reseteada, se re-identificará en el próximo 'start'.")

    if encontrados_total == 0:
        print("\n✨ No hay carpetas sospechosas ni pendientes de confirmar en la caché.")
    else:
        print("\n🩺 Revisión completa. Lanza 'comicscrapper.py start' para aplicar los reseteos.")

def main():
    try:
        config = cargar_configuracion()
        valid_commands = ["start", "review", "fix", "set-id", "doctor"]
        if len(sys.argv) < 2 or sys.argv[1].lower() not in valid_commands:
            print("\n🦇 Uso: comicscrapper.py [start|review|fix|set-id|doctor]")
            sys.exit(1)

        cmd = sys.argv[1].lower()

        if cmd == "doctor":
            comando_doctor(config)
            sys.exit(0)

        if cmd == "fix" or cmd == "set-id":
            objetivo = os.path.abspath(sys.argv[2])
            dir_base_elegido = config.get("rutas_comics", [])[0]
            for ruta in config.get("rutas_comics", []):
                if objetivo.startswith(ruta):
                    dir_base_elegido = ruta
                    break
            
            iniciar_db(dir_base_elegido)
            ruta_relativa = os.path.relpath(objetivo, dir_base_elegido)
            with DB_LOCK:
                cursor = DB_CONN.cursor()
                cursor.execute("DELETE FROM api_cache")

                if cmd == "fix":
                    if os.path.isfile(objetivo):
                        cursor.execute("DELETE FROM archivos WHERE ruta_relativa = ?", (ruta_relativa,))
                        DB_CONN.commit()
                    else:
                        DB_CONN.commit()
                        resetear_carpeta(objetivo, dir_base_elegido)
                else:
                    nuevo_id = str(sys.argv[3]).strip()
                    if os.path.isfile(objetivo):
                        cursor.execute("REPLACE INTO archivos (ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)", (ruta_relativa, nuevo_id, 0))
                    else:
                        cursor.execute("REPLACE INTO series (ruta_carpeta, cv_id, mt_id, nombre_oficial) VALUES (?, ?, ?, ?)",
                                       (ruta_relativa, nuevo_id, None, os.path.basename(objetivo)))
                    DB_CONN.commit()
            print("  ✅ Todo listo. Ejecuta 'python3 comicscrapper.py start'.")
            sys.exit(0)

        rutas_a_procesar = config.get("rutas_comics", [])
        for arg in sys.argv[2:]:
            if not arg.startswith("--"):
                rutas_a_procesar = [arg]
                break

        for directorio_base in rutas_a_procesar:
            if not os.path.exists(directorio_base): continue
            print(f"\n🚀 PROCESANDO RUTA: {directorio_base}")
            limpiar_basura(directorio_base)
            realizar_copia_seguridad_db(directorio_base)

            if cmd == "start":
                while True:
                    ejecutar_motor_procesamiento(directorio_base)
                    cp, ap = escanear_solo_pendientes(directorio_base)
                    if not lanzar_asistente_manual(cp, ap, directorio_base):
                        break
            elif cmd == "review":
                while True:
                    cp, ap = escanear_solo_pendientes(directorio_base)
                    if lanzar_asistente_manual(cp, ap, directorio_base):
                        ejecutar_motor_procesamiento(directorio_base)
                    else:
                        break

    except KeyboardInterrupt: sys.exit(0)

if __name__ == "__main__":
    main()
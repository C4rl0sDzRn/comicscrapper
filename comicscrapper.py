#!/usr/bin/env python3
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

COMICVINE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Kavita-Sync/6.5"

# --- SE LLENARÁN DESDE EL CONFIG.JSON ---
COMICVINE_API_KEY = ""
METRON_TOKEN = ""
DB_CONN = None

# ==========================================
# CONFIGURACIÓN Y TAREAS INICIALES
# ==========================================
def cargar_configuracion():
    config_path = "config.json"
    if not os.path.exists(config_path):
        print("⚠️ No se encuentra 'config.json'. Iniciando asistente...")
        sys.exit(1)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            global COMICVINE_API_KEY, METRON_TOKEN
            apikeys = config.get("api_keys", {})
            COMICVINE_API_KEY = apikeys.get("comicvine", "")
            METRON_TOKEN = apikeys.get("metron", "")
            return config
    except Exception as e:
        print(f"❌ Error leyendo config.json: {e}")
        sys.exit(1)

def limpiar_basura(directorio):
    print(f"  🧹 Limpiando archivos basura en: {directorio}...")
    basura = ['.DS_Store', 'Thumbs.db', '@eaDir', 'desktop.ini']
    for root, dirs, files in os.walk(directorio, topdown=False):
        for f in files:
            if f in basura or f.startswith('._') or f.lower().endswith(('.tmp', '.filepart')):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass

def realizar_copia_seguridad_db(directorio):
    db_path = os.path.join(directorio, ".comicscrapper.sqlite")
    bak_path = os.path.join(directorio, ".comicscrapper.sqlite.bak")
    if os.path.exists(db_path):
        print("  💾 Guardando copia de seguridad de la base de datos...")
        try:
            shutil.copy2(db_path, bak_path)
        except OSError:
            pass

def iniciar_db(directorio):
    global DB_CONN
    db_path = os.path.join(directorio, ".comicscrapper.sqlite")
    DB_CONN = sqlite3.connect(db_path)
    cursor = DB_CONN.cursor()
    
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS archivos '
        '(ruta_relativa TEXT PRIMARY KEY, cv_id TEXT, mtime REAL)'
    )
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS api_cache '
        '(url TEXT PRIMARY KEY, respuesta TEXT, timestamp REAL)'
    )
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS series '
        '(ruta_carpeta TEXT PRIMARY KEY, cv_id TEXT, mt_id TEXT, nombre_oficial TEXT)'
    )
    DB_CONN.commit()

def obtener_ids_carpeta(ruta_carpeta, directorio_base):
    ruta_relativa = os.path.relpath(ruta_carpeta, directorio_base)
    cursor = DB_CONN.cursor()
    cursor.execute("SELECT cv_id, mt_id FROM series WHERE ruta_carpeta = ?", (ruta_relativa,))
    row = cursor.fetchone()
    if row and (row[0] or row[1]):
        return row[0], row[1]

    json_path = os.path.join(ruta_carpeta, ".comic_id.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                cv_id, mt_id = data.get("cv_id"), data.get("mt_id")
                if cv_id or mt_id:
                    cursor.execute(
                        "REPLACE INTO series (ruta_carpeta, cv_id, mt_id, nombre_oficial) VALUES (?, ?, ?, ?)",
                        (ruta_relativa, cv_id, mt_id, data.get("serie", ""))
                    )
                    DB_CONN.commit()
                    return str(cv_id) if cv_id else None, str(mt_id) if mt_id else None
        except (json.JSONDecodeError, IOError):
            pass

    m_cv = re.findall(r"\[cv-(\d+)\]", os.path.basename(ruta_carpeta), re.I)
    if m_cv:
        return str(m_cv[-1]), None
    
    m_mt = re.findall(r"\[mt-(\d+)\]", os.path.basename(ruta_carpeta), re.I)
    if m_mt:
        return None, str(m_mt[-1])
    
    return None, None

def guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_id, mt_id, serie, volumen):
    json_path = os.path.join(ruta_carpeta, ".comic_id.json")
    data = {"serie": serie, "volumen": volumen, "cv_id": cv_id, "mt_id": mt_id}
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except IOError:
        pass

    ruta_relativa = os.path.relpath(ruta_carpeta, directorio_base)
    cursor = DB_CONN.cursor()
    cursor.execute(
        "REPLACE INTO series (ruta_carpeta, cv_id, mt_id, nombre_oficial) VALUES (?, ?, ?, ?)",
        (ruta_relativa, cv_id, mt_id, serie)
    )
    DB_CONN.commit()

def limpiar_nombre_busqueda(texto):
    t = texto.lower()
    t = re.sub(r'&', 'and', t)
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    return ' '.join(t.split())

def decodificar_leetspeak(texto):
    # Si no hay letras, no es un título (evita romper números reales)
    if not re.search(r'[a-zA-Z]', texto): return texto
    mapa = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't'}
    res = "".join([mapa.get(c, c) for c in texto])
    return res

def peticion_api_segura(url, max_reintentos=4, dias_cache=30):
    global DB_CONN
    cursor = DB_CONN.cursor()
    cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row and (time.time() - row[1]) < (dias_cache * 86400):
        return json.loads(row[0])

    print(f"  🌐 [CV] Consultando API…")
    for intento in range(max_reintentos):
        try:
            req = Request(url, headers={"User-Agent": COMICVINE_USER_AGENT})
            with urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8"))
                if str(resp.get("error", "OK")).upper() != "OK":
                    return resp

                is_search = "search/?" in url
                has_results = bool(resp.get("results"))
                if not is_search or has_results:
                    cursor.execute(
                        "REPLACE INTO api_cache (url, respuesta, timestamp) VALUES (?, ?, ?)",
                        (url, json.dumps(resp), time.time())
                    )
                    DB_CONN.commit()
                return resp
        except HTTPError as e:
            if e.code == 429:
                print(f"    ⏳ Rate-limit. Esperando 10 s (intento {intento+1}/{max_reintentos})…")
                time.sleep(10)
            else:
                print(f"    ⚠️  HTTP {e.code}: {e.reason}. Reintentando…")
                time.sleep(3)
        except Exception:
            time.sleep(3)
    return None

def peticion_api_metron(url, max_reintentos=3, dias_cache=30):
    global DB_CONN
    if not METRON_TOKEN:
        return None
    
    cursor = DB_CONN.cursor()
    cursor.execute("SELECT respuesta, timestamp FROM api_cache WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row and (time.time() - row[1]) < (dias_cache * 86400):
        return json.loads(row[0])

    print(f"  🌐 [Metron] Consultando API…")
    for intento in range(max_reintentos):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": COMICVINE_USER_AGENT,
                    "Authorization": f"Bearer {METRON_TOKEN}"
                }
            )
            with urlopen(req, timeout=10) as r:
                resp = json.loads(r.read().decode("utf-8"))

                is_search = "/api/series/?" in url
                has_results = bool(resp.get("results"))
                if not is_search or has_results:
                    cursor.execute(
                        "REPLACE INTO api_cache (url, respuesta, timestamp) VALUES (?, ?, ?)",
                        (url, json.dumps(resp), time.time())
                    )
                    DB_CONN.commit()
                return resp
        except Exception:
            time.sleep(2)
    return None

def procesar_candidatos(resultados, query, numero_volumen, total_comics, year_esperado, origen="cv"):
    if not resultados:
        return None, None

    # 1. Filtro estricto por nombre
    query_limpia = limpiar_nombre_busqueda(query)
    exactos = [
        v for v in resultados
        if limpiar_nombre_busqueda(v.get("name", "")) == query_limpia
    ]
    candidatos = exactos if exactos else [
        v for v in resultados
        if query_limpia in limpiar_nombre_busqueda(v.get("name", ""))
    ]

    if not candidatos:
        return None, None

    # 2. Ordenar cronológicamente
    validos = []
    for c in candidatos:
        year = c.get("start_year") if origen == "cv" else c.get("year_began")
        year_int = int(year) if year and str(year).isdigit() else 9999
        validos.append((year_int, c))
    validos.sort(key=lambda x: x[0])
    candidatos = [v[1] for v in validos]

    # 3. Emparejar por Año
    if year_esperado:
        for c in candidatos:
            year = str(c.get("start_year") if origen == "cv" else c.get("year_began"))
            if year == str(year_esperado):
                return str(c.get("id")), c.get("name")

    # 4. Deducción por Volumen
    if numero_volumen and str(numero_volumen).isdigit():
        idx = int(numero_volumen) - 1
        if 0 <= idx < len(candidatos):
            return str(candidatos[idx].get("id")), candidatos[idx].get("name")

    # 5. Deducción por cantidad de Issues
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
            return str(mejor_c.get("id")), mejor_c.get("name")

    # Fallback: devolver el primero si no hay mejor opción
    return str(candidatos[0].get("id")), candidatos[0].get("name")

def buscar_cv_id_en_comic_vine(nombre_serie, numero_volumen="1", total_comics=1, es_anual=False, es_special=False, year_esperado=None):
    if not COMICVINE_API_KEY:
        return None, None
    
    nombre_puro = re.sub(r"(?i)\bvol\.?\s*\d+", "", nombre_serie).strip()
    nombre_puro = re.sub(r"\(\d{4}\)", "", nombre_puro).strip()
    nombre_puro = decodificar_leetspeak(nombre_puro)

    if es_anual:
        query = f"{nombre_puro} Annual"
    elif es_special:
        query = f"{nombre_puro} Special"
    else:
        query = nombre_puro

    query_url = quote(re.sub(r'[^a-zA-Z0-9]', ' ', query).strip())
    url = f"https://comicvine.gamespot.com/api/search/?api_key={COMICVINE_API_KEY}&format=json&query={query_url}&resources=volume"

    data = peticion_api_segura(url)
    if data and data.get("results"):
        return procesar_candidatos(data["results"], query, numero_volumen, total_comics, year_esperado, "cv")
    return None, None

def buscar_mt_id_en_metron(nombre_serie, numero_volumen="1", total_comics=1, es_anual=False, es_special=False, year_esperado=None):
    if not METRON_TOKEN:
        return None, None
    
    nombre_puro = re.sub(r"(?i)\bvol\.?\s*\d+", "", nombre_serie).strip()
    nombre_puro = re.sub(r"\(\d{4}\)", "", nombre_puro).strip()
    nombre_puro = decodificar_leetspeak(nombre_puro)

    query_url = quote(re.sub(r'[^a-zA-Z0-9]', ' ', nombre_puro).strip())
    url = f"https://metron.cloud/api/series/?name={query_url}"

    data = peticion_api_metron(url)
    if data and data.get("results"):
        return procesar_candidatos(data["results"], nombre_puro, numero_volumen, total_comics, year_esperado, "mt")
    return None, None

def obtener_datos_volumen(cv_id):
    mapa, mapa_ann, mapa_spc = {}, {}, {}
    vol_info, pub, tot = {}, None, 0
    
    url = f"https://comicvine.gamespot.com/api/volume/4050-{cv_id}/?api_key={COMICVINE_API_KEY}&format=json&field_list=issues,publisher,name,count_of_issues"
    data = peticion_api_segura(url)
    
    if data and data.get("results"):
        res = data["results"]
        if isinstance(res, list):
            res = res[0] if res else {}
        vol_info["name"] = res.get("name")
        tot = res.get("count_of_issues") or 0
        if res.get("publisher"):
            pub = res.get("publisher").get("name")
        
        for issue in res.get("issues", []):
            num = str(issue.get("issue_number", "")).strip().lstrip("0") or "0"
            iss_id = issue.get("id")
            name_low = str(issue.get("name", "")).lower()
            url_low = str(issue.get("site_detail_url", "")).lower()
            
            if "annual" in name_low or "annual" in url_low:
                mapa_ann[num] = iss_id
            elif "special" in name_low or "special" in url_low:
                mapa_spc[num] = iss_id
            else:
                mapa[num] = iss_id
        time.sleep(0.5)
    
    return (mapa, mapa_ann, mapa_spc, vol_info, pub, tot)

def consultar_comic_vine_issue(issue_id):
    url = f"https://comicvine.gamespot.com/api/issue/4000-{issue_id}/?api_key={COMICVINE_API_KEY}&format=json"
    data = peticion_api_segura(url)
    if not data:
        return None
    
    res = data.get("results", {})
    if isinstance(res, dict):
        return res
    if isinstance(res, list) and len(res) > 0:
        return res[0]
    return {}

def obtener_datos_volumen_metron(mt_id):
    mapa, mapa_ann, mapa_spc = {}, {}, {}
    vol_info, pub, tot = {}, None, 0
    
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
                name_low = (
                    str(issue.get("issue_name", "")).lower() +
                    " " +
                    str(issue.get("name", "")).lower()
                )
                
                if "annual" in name_low:
                    mapa_ann[num] = iss_id
                elif "special" in name_low:
                    mapa_spc[num] = iss_id
                else:
                    mapa[num] = iss_id
    
    return (mapa, mapa_ann, mapa_spc, vol_info, pub, tot)

def consultar_metron_issue(issue_id):
    url = f"https://metron.cloud/api/issue/{issue_id}/"
    data = peticion_api_metron(url)
    if not data:
        return None
    
    cv_issue = {
        "issue_number": data.get("number"),
        "name": data.get("issue_name") or data.get("name"),
        "cover_date": data.get("cover_date"),
        "description": data.get("desc"),
        "site_detail_url": f"https://metron.cloud/issue/{data.get('id')}/",
        "person_credits": [],
        "character_credits": [],
        "story_arc_credits": [],
        "rated": (
            data.get("rating", {}).get("name")
            if isinstance(data.get("rating"), dict)
            else data.get("rating")
        ),
        "volume": {
            "name": (
                data.get("series", {}).get("name")
                if isinstance(data.get("series"), dict)
                else data.get("series")
            )
        }
    }
    
    for c in data.get("credits", []):
        cv_issue["person_credits"].append({
            "name": c.get("creator"),
            "role": c.get("role", {}).get("name")
        })
    
    for c in data.get("characters", []):
        cv_issue["character_credits"].append({"name": c.get("name")})
    
    for arc in data.get("arcs", []):
        cv_issue["story_arc_credits"].append({"name": arc.get("name")})
    
    return cv_issue

def resolver_issue_id_y_datos(cv_id, mt_id, numero_issue, es_annual=False, es_special=False):
    if cv_id:
        mapa, mapa_ann, mapa_spc, vol_info, pub, tot = obtener_datos_volumen(cv_id)
    elif mt_id:
        mapa, mapa_ann, mapa_spc, vol_info, pub, tot = obtener_datos_volumen_metron(mt_id)
    else:
        return None, {}, None, 0
    
    num = str(numero_issue).lstrip("0") or "0"
    if es_annual:
        return mapa_ann.get(num), vol_info, pub, tot
    if es_special:
        return mapa_spc.get(num), vol_info, pub, tot
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
    if not nombre_carpeta: return "1"
    limpio = limpiar_nombre_serie(nombre_carpeta)
    m = re.search(r"(?:vol|volume)\.?\s*(\d+)", limpio, re.I)
    if m: return m.group(1)
    nums = re.findall(r"\d+", limpio)
    return nums[-1] if nums else "1"

def asignar_id_a_carpeta_si_falta(ruta_carpeta, serie_nombre, numero_volumen, directorio_base):
    cv_id, mt_id = obtener_ids_carpeta(ruta_carpeta, directorio_base)
    if cv_id or mt_id: return cv_id, mt_id
    
    nombre_actual = os.path.basename(ruta_carpeta)
    try: total_archivos = sum(1 for f in os.listdir(ruta_carpeta) if f.lower().endswith((".cbz", ".cbr")))
    except: total_archivos = 1
    match_year = re.search(r"\((\d{4})\)", nombre_actual)
    year_carpeta = match_year.group(1) if match_year else None
    
    print(f"🔍 Evaluando carpeta: '{serie_nombre}' (Vol. {numero_volumen}) | Archivos: {total_archivos}...")
    api_id, nombre_oficial = buscar_cv_id_en_comic_vine(serie_nombre, numero_volumen, total_archivos, False, False, year_carpeta)
    fuente = "cv"
    
    if not api_id and METRON_TOKEN:
        print(f"  🔄 CV sin resultado claro. Probando en Metron (Plan B)…")
        api_id, nombre_oficial = buscar_mt_id_en_metron(serie_nombre, numero_volumen, total_archivos, False, False, year_carpeta)
        fuente = "mt" if api_id else None
        
    if api_id:
        cv_val = api_id if fuente == "cv" else None
        mt_val = api_id if fuente == "mt" else None
        guardar_metadatos_carpeta(ruta_carpeta, directorio_base, cv_val, mt_val, nombre_oficial or serie_nombre, numero_volumen)
        return cv_val, mt_val
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
    
    # Validar si ya es CBZ válido
    if ext.lower() == ".cbz" and os.path.exists(ruta_archivo):
        try:
            with zipfile.ZipFile(ruta_archivo, "r") as z:
                if z.testzip() is None:
                    return ruta_archivo
        except Exception:
            pass

    temp_dir = os.path.join(os.path.dirname(ruta_archivo), "temp_conv_real")
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)
        print(f"  🔄 Convirtiendo a CBZ: {os.path.basename(ruta_archivo)}")

        # Intentar con unrar primero
        res = subprocess.run(
            ["unrar", "e", "-inul", ruta_archivo, temp_dir + "/"],
            capture_output=True
        )
        if res.returncode != 0 or not os.listdir(temp_dir):
            subprocess.run(
                ["unzip", "-q", ruta_archivo, "-d", temp_dir + "/"],
                capture_output=True
            )

        # Recopilar archivos extraídos
        archivos_extraidos = []
        for rt, _, fnames in os.walk(temp_dir):
            for fname in sorted(fnames):
                archivos_extraidos.append(os.path.join(rt, fname))

        if not archivos_extraidos:
            shutil.rmtree(temp_dir)
            return ruta_archivo

        # Crear CBZ
        with zipfile.ZipFile(ruta_cbz, "w", zipfile.ZIP_DEFLATED) as z_out:
            for ruta_img in archivos_extraidos:
                z_out.write(ruta_img, os.path.basename(ruta_img))

        shutil.rmtree(temp_dir)
        
        # Validar CBZ
        try:
            with zipfile.ZipFile(ruta_cbz, "r") as z_check:
                if z_check.testzip() is not None:
                    raise ValueError("CBZ corrupto")
        except Exception:
            if os.path.exists(ruta_cbz):
                os.remove(ruta_cbz)
            return ruta_archivo

        os.remove(ruta_archivo)
        return ruta_cbz
    except Exception:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
    return ruta_archivo

def traducir_texto(texto):
    if not texto or len(texto.strip()) < 5:
        return texto
    try:
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=auto&tl=es&dt=t&q=" +
            quote(texto)
        )
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            return "".join([s[0] for s in data[0] if s[0]])
    except Exception:
        return texto

def inyectar_metadatos_oficiales(ruta_cbz, info_issue, serie_nombre, numero_volumen, numero_issue_forzado, publisher_volumen, total_issues, is_annual, is_special):
    try:
        root = None
        try:
            with zipfile.ZipFile(ruta_cbz, "r") as z:
                if "ComicInfo.xml" in z.namelist():
                    root = ET.fromstring(z.read("ComicInfo.xml"))
        except Exception:
            pass
        
        if root is None:
            root = ET.Element("ComicInfo")
        def set_tag(tag, val):
            if val is not None and str(val).strip():
                elem = root.find(tag)
                if elem is None: elem = ET.SubElement(root, tag)
                elem.text = str(val).strip()
                
        set_tag("Series", serie_nombre)
        if not is_annual: set_tag("Volume", str(numero_volumen))
        set_tag("Number", str(info_issue.get("issue_number") or numero_issue_forzado or "1").strip())
        if total_issues and int(total_issues) > 0: set_tag("Count", str(total_issues))
        if is_annual: set_tag("Format", "Annual")
        elif is_special: set_tag("Format", "Special")
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
        set_tag("Letterer", ", ".join(letterers))
        set_tag("Editor", ", ".join(editors))
        chars = [c.get("name") for c in info_issue.get("character_credits", []) if c.get("name")][:5]
        arcs = [arc.get("name") for arc in info_issue.get("story_arc_credits", []) if arc.get("name")]
        tags = list(set(chars + arcs + [serie_nombre]))
        set_tag("Characters", ", ".join(chars))
        set_tag("Tags", ", ".join(tags))
        if "genre" in info_issue: set_tag("Genre", info_issue["genre"])
        if arcs: set_tag("StoryArc", arcs[0])
        
        xml_content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        temp_path = ruta_cbz + ".tmp_xml"
        seen_files = set()
        with zipfile.ZipFile(ruta_cbz, "r") as z_in, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.infolist():
                if item.filename == "ComicInfo.xml" or ".DS_Store" in item.filename or item.filename.startswith("._"): continue
                if item.filename not in seen_files:
                    z_out.writestr(item, z_in.read(item.filename))
                    seen_files.add(item.filename)
            z_out.writestr("ComicInfo.xml", xml_content)
        os.replace(temp_path, ruta_cbz)
        return True, traducido, year
    except Exception as e:
        tmp = ruta_cbz + ".tmp_xml"
        if os.path.exists(tmp): os.remove(tmp)
        return False, False, None

def renombrar_archivo_nativo(ruta_cbz, serie_nombre, numero_volumen, numero_issue, year, is_annual, is_special):
    carpeta = os.path.dirname(ruta_cbz)
    parts = str(numero_issue).split(".")
    num_val = parts[0].zfill(3) + (f".{parts[1]}" if len(parts) > 1 else "")

    serie_limpia = re.sub(r"(?i)\bvol\.?\s*\d+", "", serie_nombre).strip()

    if is_annual:
        centro = f"Annual #{num_val}"
    elif is_special:
        centro = f"Special #{num_val}"
    else:
        vol_str = f"Vol {numero_volumen}" if numero_volumen else "Vol 1"
        centro = f"{vol_str} #{num_val}"

    nombre_base = f"{serie_limpia} {centro}"
    if year:
        nombre_base += f" ({year})"
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
# MOTOR PRINCIPAL
# ==========================================
def ejecutar_motor_procesamiento(directorio):
    carpetas_a_renombrar_al_final = {}
    directorio = os.path.abspath(directorio)
    iniciar_db(directorio)
    cursor = DB_CONN.cursor()
    
    print(f"\n🚀 Iniciando escaneo profundo en: {directorio}\n")
    carpetas_sin_id = []
    
    for root, dirs, files in os.walk(directorio, topdown=False):
        cbz_files = [f for f in files if f.lower().endswith((".cbz", ".cbr"))]
        if not cbz_files:
            continue
        
        root_name = os.path.basename(root).lower()
        if "annual" in root_name or "special" in root_name:
            continue
        
        cv_id, mt_id = obtener_ids_carpeta(root, directorio)
        sb, _ = obtener_serie_y_volumen(root, directorio)
        sb_limpio = limpiar_nombre_serie(sb)
        nombre_actual = os.path.basename(root)
        
        if cv_id or mt_id:
            if not os.path.exists(os.path.join(root, ".comic_id.json")):
                guardar_metadatos_carpeta(root, directorio, cv_id, mt_id, sb_limpio, extraer_numero_volumen(nombre_actual))
        else:
            res_cv, res_mt = asignar_id_a_carpeta_si_falta(
                root, sb_limpio, extraer_numero_volumen(nombre_actual), directorio
            )
            if not res_cv and not res_mt:
                carpetas_sin_id.append((
                    root, sb_limpio, extraer_numero_volumen(nombre_actual)
                ))
                
    for root, _, files in os.walk(directorio):
        nombre_carpeta_actual = os.path.basename(root)
        for file in sorted(files):
            r_orig = os.path.join(root, file)
            fl = file.lower()
            
            # Omitir archivos que no son comics o que ya están procesados
            if fl.endswith((".cbz", ".cbr")):
                skip_patterns = ["traducido", "spanish", "crg", "custom", ".tmp"]
                if any(p in fl for p in skip_patterns):
                    continue
            else:
                continue
            
            sb, v_txt = obtener_serie_y_volumen(root, directorio)
            sb_limpia = limpiar_nombre_serie(sb)
            sb_pura = re.sub(r"(?i)\bvol\.?\s*\d+", "", sb_limpia).strip()
            v_num = extraer_numero_volumen(nombre_carpeta_actual)
            
            cv_id_carp, mt_id_carp = obtener_ids_carpeta(root, directorio)
            iss_id_exp = extraer_issue_id_explicito(file)
            mtime_act = os.path.getmtime(r_orig)
            ruta_relativa = os.path.relpath(r_orig, directorio)
            
            cursor.execute(
                "SELECT cv_id, mtime FROM archivos WHERE ruta_relativa = ?",
                (ruta_relativa,)
            )
            row = cursor.fetchone()
            db_cv_id = None

            if row:
                db_cv_id, db_mtime = row
                if abs(db_mtime - mtime_act) < 5:
                    continue

            if not iss_id_exp and db_cv_id and db_cv_id.isdigit():
                iss_id_exp = db_cv_id

            print(f"🎯 Procesando: {file}")
            try:
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
                        _, _, _, pub_v, tot_iss = obtener_datos_volumen(info["volume"]["id"])
                        info_iss = info
                else:
                    iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(cv_id_carp, mt_id_carp, n_iss, is_ann, is_spc)
                    fuente = "cv" if cv_id_carp else "mt"
                    
                    if not iss_id and cv_id_carp and METRON_TOKEN:
                        mt_fallback = mt_id_carp
                        if not mt_fallback: mt_fallback, _ = buscar_mt_id_en_metron(sb_pura, v_num, 1, is_ann, is_spc)
                        if mt_fallback:
                            iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(None, mt_fallback, n_iss, is_ann, is_spc)
                            if iss_id:
                                fuente = "mt"
                                if not mt_id_carp: guardar_metadatos_carpeta(root, directorio, cv_id_carp, mt_fallback, sb_pura, v_num)
                    
                    if not iss_id and (is_ann or is_spc):
                        v_ext_id_cv, _ = buscar_cv_id_en_comic_vine(sb_pura, "", 1, is_ann, is_spc)
                        if v_ext_id_cv:
                            iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(v_ext_id_cv, None, n_iss, is_ann, is_spc)
                            fuente = "cv"
                        elif METRON_TOKEN:
                            v_ext_id_mt, _ = buscar_mt_id_en_metron(sb_pura, "", 1, is_ann, is_spc)
                            if v_ext_id_mt:
                                iss_id, info_iss_tmp, pub_v, tot_iss = resolver_issue_id_y_datos(None, v_ext_id_mt, n_iss, is_ann, is_spc)
                                fuente = "mt"
                
                nombre_oficial_serie = sb_pura
                if iss_id:
                    if fuente == "cv":
                        info_iss = consultar_comic_vine_issue(iss_id) or {}
                        if info_iss.get("volume") and info_iss["volume"].get("name"):
                            nombre_oficial_serie = info_iss["volume"]["name"]
                    else:
                        info_iss = consultar_metron_issue(iss_id) or {}
                        if info_iss.get("volume") and info_iss["volume"].get("name"):
                            nombre_oficial_serie = info_iss["volume"]["name"]

                r_orig = convertir_real_cbr_a_cbz(r_orig)

                if iss_id and info_iss:
                    ok, trad, year = inyectar_metadatos_oficiales(r_orig, info_iss, nombre_oficial_serie, v_num, n_iss, pub_v, tot_iss, is_ann, is_spc)
                    if ok:
                        if trad: print("  🇪🇸 Summary traducido al español.")
                        r_orig, renombrado = renombrar_archivo_nativo(r_orig, nombre_oficial_serie, v_num, n_iss, year, is_ann, is_spc)
                        if renombrado: print(f"  ✏️  → {os.path.basename(r_orig)}")
                        print(f"  ✅ Metadatos oficiales ({fuente.upper()}) inyectados.\n")
                        
                        ruta_relativa_final = os.path.relpath(r_orig, directorio)
                        cursor.execute("REPLACE INTO archivos (ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)",
                                       (ruta_relativa_final, iss_id_exp or iss_id, os.path.getmtime(r_orig)))
                        DB_CONN.commit()
                        carpetas_a_renombrar_al_final[root] = (nombre_oficial_serie, v_num)
                    else:
                        print(f"  ❌ Fallo al escribir XML.\n")
                else:
                    print(f"  🟡 Sin ID exacto de issue. Pendiente de revisión manual.\n")
            except KeyboardInterrupt:
                print(
                    "\n\n🛑 Búsqueda cancelada por el usuario. "
                    "Guardando estado y saliendo de forma segura..."
                )
                DB_CONN.commit()
                raise
            except Exception as e:
                print(f"❌ Error crítico: {e}\n")
            
    if carpetas_a_renombrar_al_final:
        print(
            "\n📁 Optimizando nombres de carpetas "
            "según metadatos oficiales..."
        )
        for old_root, (true_name, vol) in carpetas_a_renombrar_al_final.items():
            root_name = os.path.basename(old_root).lower()
            if "annual" in root_name or "special" in root_name:
                continue
            
            nombre_limpio = re.sub(r'[\\/:*?"<>|]', '-', true_name).strip()
            vol_str = f" Vol 1" if not vol else f" Vol {vol}"
            nuevo_nombre = f"{nombre_limpio}{vol_str}"
            nuevo_root = os.path.join(os.path.dirname(old_root), nuevo_nombre)

            if old_root != nuevo_root and not os.path.exists(nuevo_root):
                try:
                    os.rename(old_root, nuevo_root)
                    print(f"  ✅ Carpeta corregida: '{nuevo_nombre}'")
                    old_rel = os.path.relpath(old_root, directorio)
                    new_rel = os.path.relpath(nuevo_root, directorio)
                    cursor.execute(
                        "UPDATE series SET ruta_carpeta = ?, nombre_oficial = ? "
                        "WHERE ruta_carpeta = ?",
                        (new_rel, true_name, old_rel)
                    )
                    cursor.execute(
                        "UPDATE archivos SET ruta_relativa = REPLACE("
                        "ruta_relativa, ?, ?) WHERE ruta_relativa LIKE ?",
                        (old_rel, new_rel, old_rel + "%")
                    )
                    DB_CONN.commit()
                except OSError:
                    pass
                    
    return carpetas_sin_id

def escanear_solo_pendientes(directorio):
    print(
        f"\n🔍 Revisando elementos pendientes sin hacer llamadas "
        f"a internet (modo rápido)..."
    )
    iniciar_db(directorio)
    cursor = DB_CONN.cursor()
    carpetas_pendientes, archivos_pendientes = [], []

    for root, dirs, files in os.walk(directorio):
        cbz_files = [f for f in files if f.lower().endswith((".cbz", ".cbr"))]
        if not cbz_files:
            continue
        
        nc = os.path.basename(root)
        is_annual_folder = "annual" in nc.lower() or "special" in nc.lower()
        cv_id_carp, mt_id_carp = obtener_ids_carpeta(root, directorio)

        if not cv_id_carp and not mt_id_carp and not is_annual_folder:
            sb, _ = obtener_serie_y_volumen(root, directorio)
            carpetas_pendientes.append((
                root,
                limpiar_nombre_serie(sb),
                extraer_numero_volumen(nc)
            ))

        for f in sorted(cbz_files):
            fl = f.lower()
            skip_patterns = ["traducido", "spanish", "crg", "custom", ".tmp"]
            if any(p in fl for p in skip_patterns):
                continue
            
            ruta_relativa = os.path.relpath(os.path.join(root, f), directorio)
            cursor.execute(
                "SELECT cv_id FROM archivos WHERE ruta_relativa = ?",
                (ruta_relativa,)
            )
            if not cursor.fetchone():
                archivos_pendientes.append(os.path.join(root, f))

    return carpetas_pendientes, archivos_pendientes

def lanzar_asistente_manual(carpetas_pendientes, archivos_pendientes, directorio_base):
    try:
        if not carpetas_pendientes and not archivos_pendientes:
            return
        
        cambios = False

        if carpetas_pendientes:
            print("\n" + "=" * 50)
            print(
                f"🤔 Hay {len(carpetas_pendientes)} serie(s) pendiente(s) "
                f"de identificar."
            )
            
            respuesta = (
                input(
                    "¿Deseas identificar series manualmente ahora? (S/N): "
                )
                .strip()
                .lower()
            )
            
            if respuesta == "s":
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
                        q = (
                            input(
                                f"   🌐 Búsqueda CV ['{s_base} vol {n_vol}']: "
                            ).strip() or f"{s_base} vol {n_vol}"
                        )
                        res = peticion_api_segura(
                            f"https://comicvine.gamespot.com/api/search/"
                            f"?api_key={COMICVINE_API_KEY}&format=json"
                            f"&query={quote(q)}&resources=volume"
                        )
                        if res and res.get("results"):
                            for i, v in enumerate(res["results"][:5]):
                                print(
                                    f"     [{i+1}] {v.get('name')} "
                                    f"({v.get('start_year')}) "
                                    f"[ID: {v.get('id')}]"
                                )
                            sel = (
                                input(
                                    "\n   Elige número de la lista "
                                    "(0 para omitir): "
                                )
                                .strip()
                            )
                            if sel.isdigit() and 1 <= int(sel) <= 5:
                                cv_id = str(
                                    res["results"][int(sel)-1].get("id")
                                )
                        else:
                            print("   ⚠️ Sin resultados.")
                    elif accion == "2" and METRON_TOKEN:
                        q = (
                            input(
                                f"   🌐 Búsqueda Metron ['{s_base} vol {n_vol}']: "
                            ).strip() or f"{s_base} vol {n_vol}"
                        )
                        res = peticion_api_metron(
                            f"https://metron.cloud/api/series/?name={quote(q)}"
                        )
                        if res and res.get("results"):
                            for i, v in enumerate(res["results"][:5]):
                                print(
                                    f"     [{i+1}] {v.get('name')} "
                                    f"({v.get('year_began')}) "
                                    f"[ID: {v.get('id')}]"
                                )
                            sel = (
                                input(
                                    "\n   Elige número de la lista "
                                    "(0 para omitir): "
                                )
                                .strip()
                            )
                            if sel.isdigit() and 1 <= int(sel) <= 5:
                                mt_id = str(
                                    res["results"][int(sel)-1].get("id")
                                )
                        else:
                            print("   ⚠️ Sin resultados en Metron.")
                        
                    if (cv_id and cv_id.isdigit()) or (mt_id and mt_id.isdigit()):
                        guardar_metadatos_carpeta(r_carp, directorio_base, cv_id, mt_id, nombre_oficial, n_vol)
                        cambios = True

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
                            cursor = DB_CONN.cursor()
                            cursor.execute("REPLACE INTO archivos (ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)", (ruta_relativa, issue_id, 0))
                            is_ann_spc = "annual" in r_archivo.lower() or "special" in r_archivo.lower()
                            if not is_ann_spc:
                                n_vol = extraer_numero_volumen(os.path.basename(carpeta_actual))
                                guardar_metadatos_carpeta(carpeta_actual, directorio_base, cv_vol_id, mt_vol_id, nombre_serie_oficial or os.path.basename(carpeta_actual), n_vol)
                            DB_CONN.commit()
                            cambios = True
        if cambios: print("\n🔄 Hay cambios en cola. Vuelve a ejecutar 'python3 comicscrapper.py start'.")
    except KeyboardInterrupt: sys.exit(0)

def main():
    try:
        config = cargar_configuracion()
        
        valid_commands = ["start", "review", "fix", "set-id"]
        if (
            len(sys.argv) < 2 or
            sys.argv[1].lower() not in valid_commands
        ):
            print(
                "\n🦇 Uso: comicscrapper.py "
                "[start|review|fix|set-id]"
            )
            sys.exit(1)

        cmd = sys.argv[1].lower()

        if cmd == "fix" or cmd == "set-id":
            objetivo = os.path.abspath(sys.argv[2])
            dir_base_elegido = config.get("rutas_comics", [])[0]
            for ruta in config.get("rutas_comics", []):
                if objetivo.startswith(ruta):
                    dir_base_elegido = ruta
                    break
            
            iniciar_db(dir_base_elegido)
            cursor = DB_CONN.cursor()
            ruta_relativa = os.path.relpath(objetivo, dir_base_elegido)
            cursor.execute("DELETE FROM api_cache")

            if cmd == "fix":
                if os.path.isfile(objetivo):
                    cursor.execute(
                        "DELETE FROM archivos WHERE ruta_relativa = ?",
                        (ruta_relativa,)
                    )
                else:
                    json_path = os.path.join(objetivo, ".comic_id.json")
                    if os.path.exists(json_path):
                        os.remove(json_path)
                    cursor.execute(
                        "DELETE FROM series WHERE ruta_carpeta = ?",
                        (ruta_relativa,)
                    )
                    cursor.execute(
                        "DELETE FROM archivos WHERE ruta_relativa LIKE ?",
                        (ruta_relativa + "%",)
                    )
            else:
                nuevo_id = str(sys.argv[3]).strip()
                if os.path.isfile(objetivo):
                    cursor.execute(
                        "REPLACE INTO archivos "
                        "(ruta_relativa, cv_id, mtime) VALUES (?, ?, ?)",
                        (ruta_relativa, nuevo_id, 0)
                    )
                else:
                    cursor.execute(
                        "REPLACE INTO series "
                        "(ruta_carpeta, cv_id, mt_id, nombre_oficial) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            ruta_relativa, nuevo_id, None,
                            os.path.basename(objetivo)
                        )
                    )

            DB_CONN.commit()
            print(
                "  ✅ Todo listo. Ejecuta "
                "'python3 comicscrapper.py start'."
            )
            sys.exit(0)

        rutas_a_procesar = config.get("rutas_comics", [])
        for arg in sys.argv[2:]:
            if not arg.startswith("--"):
                rutas_a_procesar = [arg]
                break

        for directorio_base in rutas_a_procesar:
            if not os.path.exists(directorio_base):
                continue
            print(f"\n🚀 PROCESANDO RUTA: {directorio_base}")
            limpiar_basura(directorio_base)
            realizar_copia_seguridad_db(directorio_base)

            if cmd == "start":
                ejecutar_motor_procesamiento(directorio_base)
                cp, ap = escanear_solo_pendientes(directorio_base)
                lanzar_asistente_manual(cp, ap, directorio_base)
            elif cmd == "review":
                cp, ap = escanear_solo_pendientes(directorio_base)
                lanzar_asistente_manual(cp, ap, directorio_base)

    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()

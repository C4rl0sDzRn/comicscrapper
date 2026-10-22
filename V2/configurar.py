#!/usr/bin/env python3
"""
configurar.py
-------------
Asistente de configuración interactivo para comicscrapper. Crea y edita
el archivo config.json (API keys, rutas, rendimiento, carátulas y opciones
generales) de forma completamente integrada.
"""

import json
import os
import sys

CONFIG_FILE = "config.json"

def setup_inicial():
    """Asistente guiado que crea config.json la primera vez que se ejecuta."""
    print("\n" + "=" * 50)
    print("🚀 ASISTENTE DE CONFIGURACIÓN INICIAL")
    print("=" * 50)
    print("Parece que es la primera vez que ejecutas el script o falta el archivo config.json.\n")

    try:
        cv_key = ""
        while not cv_key:
            cv_key = input("👉 Introduce tu API Key de Comic Vine (OBLIGATORIA): ").strip()

        print("\n(La API de Metron es opcional. Pulsa ENTER para omitirla por ahora)")
        metron_username = input("👉 Usuario / Token de Metron: ").strip()

        ruta = ""
        while not ruta:
            ruta = input(
                "\n👉 Introduce la ruta principal de tus cómics\n"
                "   (ej. /media/sda1/servidor/media/comics): "
            ).strip()

        config = {
            "watchdog_enabled": False,
            "buscador_principal": "comicvine",
            "forzar_copia_seguridad": True,
            "borrar_basura": True,
            "rutas_comics": [ruta],
            "api_keys": {
                "comicvine": cv_key,
                "metron": metron_username,
            },
            "caratulas": {
                "descargar_caratulas_hq": True,
                "guardar_como": "cbz_interno"
            },
            "rendimiento": {
                "modo_escaneo_multiple": True,
                "hilos_simultaneos": 4,
                "pausa_entre_peticiones_api": 1.5
            }
        }

        guardar_config(config)
        print("\n✅ Configuración inicial guardada correctamente.")
        return config

    except KeyboardInterrupt:
        print("\n\n👋 Operación cancelada por el usuario. Saliendo del asistente...")
        sys.exit(0)

def cargar_config():
    """Carga config.json asegurando que contenga todas las claves nuevas."""
    if not os.path.exists(CONFIG_FILE):
        return setup_inicial()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            
            # Autocorrección / Inyección de claves faltantes si el json es antiguo
            actualizado = False
            if "caratulas" not in config:
                config["caratulas"] = {"descargar_caratulas_hq": True, "guardar_como": "cbz_interno"}
                actualizado = True
            if "rendimiento" not in config:
                config["rendimiento"] = {"modo_escaneo_multiple": True, "hilos_simultaneos": 4, "pausa_entre_peticiones_api": 1.5}
                actualizado = True
            if actualizado:
                guardar_config(config)
                
            return config
    except json.JSONDecodeError as e:
        print(f"\n❌ El archivo '{CONFIG_FILE}' existe pero está corrupto o mal formado: {e}")
        print("   Revísalo a mano, o bórralo para volver a generarlo con el asistente.")
        sys.exit(1)

def guardar_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

def gestionar_rutas(config):
    while True:
        print("\n--- GESTIÓN DE RUTAS DE CÓMICS ---")
        rutas = config.get("rutas_comics", [])
        if not rutas:
            print("  (No hay rutas configuradas)")
        else:
            for i, r in enumerate(rutas):
                print(f"  {i + 1}. {r}")

        print("\nOpciones:")
        print("1. Añadir nueva ruta")
        print("2. Eliminar una ruta")
        print("3. Volver al menú principal")

        opc = input("Elige una opción: ").strip()

        if opc == "1":
            nueva_ruta = input("Introduce la ruta completa: ").strip()
            if nueva_ruta and nueva_ruta not in rutas:
                config["rutas_comics"].append(nueva_ruta)
                guardar_config(config)
                print("✅ Ruta añadida.")

        elif opc == "2":
            idx = input("Introduce el número de la ruta a borrar: ").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(rutas):
                borrada = config["rutas_comics"].pop(int(idx) - 1)
                guardar_config(config)
                print(f"🗑️ Ruta borrada: {borrada}")
            else:
                print("⚠️ Número fuera de rango.")

        elif opc == "3":
            break
        else:
            print("⚠️ Opción no reconocida.")

def gestionar_apis(config):
    while True:
        print("\n--- GESTIÓN DE API KEYS ---")
        apis = config.get("api_keys", {})
        print(f"1. Comic Vine : {apis.get('comicvine', 'NO CONFIGURADA')}")
        print(f"2. Metron     : {apis.get('metron', 'NO CONFIGURADA')}")
        print("3. Volver al menú principal")

        opc = input("Elige la opción que quieres modificar: ").strip()

        if opc == "1":
            config["api_keys"]["comicvine"] = input("Nueva API Comic Vine: ").strip()
            guardar_config(config)
        elif opc == "2":
            config["api_keys"]["metron"] = input("Nuevo usuario/token de Metron: ").strip()
            guardar_config(config)
        elif opc == "3":
            break
        else:
            print("⚠️ Opción no reconocida.")

def gestionar_rendimiento(config):
    while True:
        rend = config.get("rendimiento", {"modo_escaneo_multiple": True, "hilos_simultaneos": 4, "pausa_entre_peticiones_api": 1.5})
        print("\n--- GESTIÓN DE RENDIMIENTO Y MULTIHILO ---")
        print(f"1. [ {'X' if rend.get('modo_escaneo_multiple') else ' '} ] Modo Multihilo simultáneo")
        print(f"2. Hilos simultáneos (Núcleos): {rend.get('hilos_simultaneos')}")
        print(f"3. Pausa entre peticiones API (segundos): {rend.get('pausa_entre_peticiones_api')}")
        print("4. Volver al menú principal")

        opc = input("Elige una opción: ").strip()
        if opc == "1":
            rend["modo_escaneo_multiple"] = not rend.get("modo_escaneo_multiple", False)
            guardar_config(config)
        elif opc == "2":
            h = input("Introduce número de hilos (ej. 2, 4, 8): ").strip()
            if h.isdigit() and int(h) > 0:
                rend["hilos_simultaneos"] = int(h)
                guardar_config(config)
        elif opc == "3":
            p = input("Introduce pausa en segundos (ej. 1.0, 1.5): ").strip()
            try:
                rend["pausa_entre_peticiones_api"] = float(p)
                guardar_config(config)
            except ValueError:
                print("⚠️ Valor inválido.")
        elif opc == "4":
            break
        else:
            print("⚠️ Opción no reconocida.")

def gestionar_caratulas(config):
    while True:
        car = config.get("caratulas", {"descargar_caratulas_hq": True, "guardar_como": "cbz_interno"})
        print("\n--- GESTIÓN DE CARÁTULAS HQ ---")
        print(f"1. [ {'X' if car.get('descargar_caratulas_hq') else ' '} ] Descargar carátulas de alta calidad")
        print(f"2. Método de guardado: {car.get('guardar_como').upper()} (cbz_interno / carpeta_externa)")
        print("3. Volver al menú principal")

        opc = input("Elige una opción: ").strip()
        if opc == "1":
            car["descargar_caratulas_hq"] = not car.get("descargar_caratulas_hq", False)
            guardar_config(config)
        elif opc == "2":
            modo = input("Introduce modo ('cbz_interno' o 'carpeta_externa'): ").strip().lower()
            if modo in ["cbz_interno", "carpeta_externa"]:
                car["guardar_como"] = modo
                guardar_config(config)
            else:
                print("⚠️ Opción no válida.")
        elif opc == "3":
            break
        else:
            print("⚠️ Opción no reconocida.")

def borrar_base_de_datos(config):
    rutas = config.get("rutas_comics", [])
    if not rutas:
        print("\nℹ️ No hay rutas configuradas.")
        return

    for ruta in rutas:
        db_file = os.path.join(ruta, ".comicscrapper.sqlite")
        bak_file = os.path.join(ruta, ".comicscrapper.sqlite.bak")
        
        if not os.path.exists(db_file) and not os.path.exists(bak_file):
            print(f"\nℹ️ No existe base de datos SQLite en {ruta}.")
            continue

        print(f"\n⚠️ ATENCIÓN: Se va a borrar la base de datos de {ruta}")
        conf = input("¿Estás SEGURO? (Escribe 'BORRAR' para confirmar): ")
        if conf != "BORRAR":
            continue
        conf2 = input("Última confirmación: ¿Seguro? (S/N): ")
        if conf2.strip().lower() == "s":
            try:
                if os.path.exists(db_file): os.remove(db_file)
                if os.path.exists(bak_file): os.remove(bak_file)
                print("✅ Base de datos borrada correctamente.")
            except OSError as e:
                print(f"❌ No se pudo borrar el archivo: {e}")

def menu_config():
    try:
        config = cargar_config()
        while True:
            print("\n" + "=" * 40)
            print("⚙️ PANEL DE CONTROL PRINCIPAL")
            print("=" * 40)
            print(f"1. [ {'X' if config.get('watchdog_enabled') else ' '} ] Activar/Desactivar Watchdog")
            print(f"2. Buscador principal: {config.get('buscador_principal', 'comicvine').upper()}")
            print(f"3. [ {'X' if config.get('forzar_copia_seguridad') else ' '} ] Copia de seguridad automática")
            print(f"4. [ {'X' if config.get('borrar_basura') else ' '} ] Limpieza automática de basura")
            print(f"5. Gestionar Rutas de Cómics ({len(config.get('rutas_comics', []))} configuradas)")
            print(f"6. Gestionar API Keys")
            print(f"7. Gestionar Rendimiento / Multihilo")
            print(f"8. Gestionar Carátulas HQ")
            print(f"9. Borrar Base de Datos local (Peligro)")
            print(f"10. Salir")
            print("=" * 40)

            opcion = input("👉 Elige una opción: ").strip()

            if opcion == "1":
                config["watchdog_enabled"] = not config.get("watchdog_enabled", False)
            elif opcion == "2":
                nuevo_buscador = input("Introduce nuevo buscador (ej. comicvine, metron): ").strip().lower()
                if nuevo_buscador:
                    config["buscador_principal"] = nuevo_buscador
            elif opcion == "3":
                config["forzar_copia_seguridad"] = not config.get("forzar_copia_seguridad", False)
            elif opcion == "4":
                config["borrar_basura"] = not config.get("borrar_basura", False)
            elif opcion == "5":
                gestionar_rutas(config)
            elif opcion == "6":
                gestionar_apis(config)
            elif opcion == "7":
                gestionar_rendimiento(config)
            elif opcion == "8":
                gestionar_caratulas(config)
            elif opcion == "9":
                borrar_base_de_datos(config)
            elif opcion == "10":
                guardar_config(config)
                print("👋 Saliendo. Configuración guardada.")
                break
            else:
                print("⚠️ Opción no reconocida.")

            guardar_config(config)

    except KeyboardInterrupt:
        print("\n\n👋 Saliendo del panel de configuración. ¡Hasta pronto!")
        sys.exit(0)

if __name__ == "__main__":
    menu_config()
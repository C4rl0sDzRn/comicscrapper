#!/usr/bin/env python3
import json
import os
import sys

CONFIG_FILE = "config.json"

def setup_inicial():
    print("\n" + "="*50)
    print("🚀 ASISTENTE DE CONFIGURACIÓN INICIAL")
    print("="*50)
    print("Parece que es la primera vez que ejecutas el script o falta el archivo config.json.\n")
    
    try:
        cv_key = ""
        while not cv_key:
            cv_key = input("👉 Introduce tu API Key de Comic Vine (OBLIGATORIA): ").strip()
        
        print("\n(La API de Metron es opcional. Pulsa ENTER para omitirla por ahora)")
        metron_username = input("👉 Usuario / Token de Metron: ").strip()
        
        ruta = ""
        while not ruta:
            ruta = input("\n👉 Introduce la ruta principal de tus cómics\n   (ej. /media/sda1/servidor/media/comics): ").strip()
        
        config = {
            "watchdog_enabled": False,
            "buscador_principal": "comicvine",
            "forzar_copia_seguridad": True,
            "borrar_basura": True,
            "rutas_comics": [ruta],
            "api_keys": {
                "comicvine": cv_key,
                "metron": metron_username
            }
        }
        
        with open(CONFIG_FILE, "w", encoding='utf-8') as f:
            json.dump(config, f, indent=4)
            
        print("\n✅ Configuración inicial guardada correctamente.")
        return config
    except KeyboardInterrupt:
        print("\n\n👋 Operación cancelada por el usuario. Saliendo del asistente...")
        sys.exit(0)

def cargar_config():
    if not os.path.exists(CONFIG_FILE):
        return setup_inicial()
    with open(CONFIG_FILE, "r", encoding='utf-8') as f:
        return json.load(f)

def guardar_config(config):
    with open(CONFIG_FILE, "w", encoding='utf-8') as f:
        json.dump(config, f, indent=4)

def gestionar_rutas(config):
    while True:
        print("\n--- GESTIÓN DE RUTAS DE CÓMICS ---")
        rutas = config.get("rutas_comics", [])
        if not rutas:
            print("  (No hay rutas configuradas)")
        else:
            for i, r in enumerate(rutas):
                print(f"  {i+1}. {r}")
                
        print("\nOpciones:")
        print("1. Añadir nueva ruta")
        print("2. Eliminar una ruta")
        print("3. Volver al menú principal")
        
        opc = input("Elige una opción: ").strip()
        if opc == '1':
            nueva_ruta = input("Introduce la ruta completa: ").strip()
            if nueva_ruta and nueva_ruta not in rutas:
                config["rutas_comics"].append(nueva_ruta)
                guardar_config(config)
                print("✅ Ruta añadida.")
        elif opc == '2':
            idx = input("Introduce el número de la ruta a borrar: ").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(rutas):
                borrada = config["rutas_comics"].pop(int(idx)-1)
                guardar_config(config)
                print(f"🗑️ Ruta borrada: {borrada}")
        elif opc == '3':
            break

def gestionar_apis(config):
    while True:
        print("\n--- GESTIÓN DE API KEYS ---")
        apis = config.get("api_keys", {})
        print(f"1. Comic Vine : {apis.get('comicvine', 'NO CONFIGURADA')}")
        print(f"2. Metron     : {apis.get('metron', 'NO CONFIGURADA')}")
        print("3. Volver al menú principal")
        
        opc = input("Elige la opción que quieres modificar: ").strip()
        if opc == '1':
            config["api_keys"]["comicvine"] = input("Nueva API Comic Vine: ").strip()
        elif opc == '2':
            config["api_keys"]["metron"] = input("Nuevo usuario/token de Metron: ").strip()
        elif opc == '3':
            guardar_config(config)
            break
        guardar_config(config)

def menu_config():
    try:
        config = cargar_config()
        while True:
            print("\n" + "="*40)
            print("⚙️ PANEL DE CONTROL PRINCIPAL")
            print("="*40)
            print(f"1. [ {'X' if config.get('watchdog_enabled') else ' '} ] Activar/Desactivar Watchdog")
            print(f"2. Buscador principal: {config.get('buscador_principal').upper()}")
            print(f"3. [ {'X' if config.get('forzar_copia_seguridad') else ' '} ] Copia de seguridad automática")
            print(f"4. [ {'X' if config.get('borrar_basura') else ' '} ] Limpieza automática de basura")
            print(f"5. Gestionar Rutas de Cómics ({len(config.get('rutas_comics', []))} configuradas)")
            print("6. Gestionar API Keys")
            print("7. Borrar Base de Datos local (Peligro)")
            print("8. Salir")
            print("="*40)
            
            opcion = input("👉 Elige una opción: ").strip()
            
            if opcion == "1":
                config['watchdog_enabled'] = not config['watchdog_enabled']
            elif opcion == "2":
                nuevo_buscador = input("Introduce nuevo buscador (ej. comicvine, metron): ").strip().lower()
                if nuevo_buscador: config['buscador_principal'] = nuevo_buscador
            elif opcion == "3":
                config['forzar_copia_seguridad'] = not config['forzar_copia_seguridad']
            elif opcion == "4":
                config['borrar_basura'] = not config['borrar_basura']
            elif opcion == "5":
                gestionar_rutas(config)
            elif opcion == "6":
                gestionar_apis(config)
            elif opcion == "7":
                rutas = config.get("rutas_comics", [])
                for ruta in rutas:
                    db_file = os.path.join(ruta, ".comics_db.json")
                    if os.path.exists(db_file):
                        print(f"\n⚠️ ATENCIÓN: Se va a borrar {db_file}")
                        conf = input("¿Estás SEGURO? (Escribe 'BORRAR' para confirmar): ")
                        if conf == 'BORRAR':
                            conf2 = input("Última confirmación: ¿Seguro? (S/N): ")
                            if conf2.lower() == 's':
                                os.remove(db_file)
                                print("✅ Base de datos borrada.")
                    else:
                        print(f"\nℹ️ No existe base de datos en {ruta}.")
            elif opcion == "8":
                guardar_config(config)
                print("👋 Saliendo. Configuración guardada.")
                break
                
            guardar_config(config)
    except KeyboardInterrupt:
        print("\n\n👋 Saliendo del panel de configuración. ¡Hasta pronto!")
        sys.exit(0)

if __name__ == "__main__":
    menu_config()

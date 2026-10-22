# ComicScrapper

## Qué es
Script en Python que exporta metadatos en español a archivos **CBZ**, usando las APIs de **Comic Vine** y **Metron**.

## Versiones
- `V2/` — **versión actual** (1579 líneas). Aquí se trabaja.
- `Backup/` — versión anterior (1503 líneas). Solo referencia, no tocar.

## Arrancar
```powershell
cd V2
python configurar.py      # primera vez: pide claves de API y rutas
python comicscrapper.py   # ejecuta el scraper
```

## Reglas
- `configurar.py` guarda tus claves de API en local: **nunca** subir esa configuración ni pegarlas en el chat.
- Solo biblioteca estándar salvo que `V2/` ya use algo. No añadir dependencias sin motivo.
- Los CBZ originales no se tocan: el script trabaja sobre copias.

# comicscrapper
ComicScrapper Pro
Un ecosistema de herramientas en Python diseñado para la automatización, organización y enriquecimiento de metadatos de colecciones de cómics digitales. Este proyecto integra APIs externas, gestión de bases de datos y procesamiento de archivos para asegurar que tu biblioteca esté siempre actualizada y bien organizada.

Características Principales
Integración de APIs: Sincronización automática de metadatos mediante las APIs de Comic Vine y Metron.

Gestión de Archivos: Conversión automatizada de diversos formatos de archivo a CBZ.

Enriquecimiento de Metadatos: Inyección automática de metadatos en formato XML dentro de los archivos.

Gestión de Caché: Base de datos local para optimizar las consultas y reducir la carga en las APIs externas.


Instalación
Clona este repositorio:

Bash
git clone https://github.com/tu-usuario/comic-scrapper.git
cd comic-scrapper

Uso
Para ejecutar el script principal de procesamiento de tu colección, utiliza:

Bash
python comicscrapper.py --path "/ruta/a/tus/comics"
Estructura del Proyecto
comicscrapper.py: Motor principal de procesamiento y scraping.

database.py: Gestión de la capa de persistencia y caché.

metadata_processor.py: Módulo encargado de la inyección de XML y normalización.

utils/: Scripts auxiliares para la gestión de archivos y rutas.

Contribuciones
Este proyecto es una herramienta personal desarrollada para optimizar la gestión de mi biblioteca digital de cómics. Cualquier sugerencia de mejora o reporte de errores es bienvenido a través de Issues.

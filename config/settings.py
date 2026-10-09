# config/settings.py
# Parámetros del proyecto. NO colocar secretos aquí (claves NVD, GitHub):
# esas van en variables de entorno (documento v2, sección 4.9).
from pathlib import Path

# Rutas (relativas a la raíz del proyecto, no al directorio desde donde se ejecuta)
RAIZ = Path(__file__).resolve().parent.parent
RUTA_SOURCES = RAIZ / "config" / "sources.yaml"
RUTA_BD = RAIZ / "storage" / "database.sqlite"  # excluida de git en .gitignore

# Normalización de URLs (usado por core/ingestion/normalizer.py)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
TRACKING_PREFIXES = ("utm_",)

# Cliente HTTP (usado por core/ingestion/http_client.py)
# Valores iniciales propuestos por mí; el documento v2 no fija estos números.
BACKOFF_BASE_SEGUNDOS = 1.0
BACKOFF_MAX_SEGUNDOS = 60.0
MAX_REDIRECCIONES = 5
CODIGOS_REINTENTABLES = {429, 500, 502, 503, 504}

# Colector CISA KEV (usado por core/ingestion/api_collector.py)
# El catálogo KEV no trae una URL por entrada; se usa la ficha del CVE en NVD
# como enlace de referencia (decisión mía, pendiente de validar con el equipo).
URL_DETALLE_CVE = "https://nvd.nist.gov/vuln/detail/{cve}"

# Orquestador (usado por core/ingestion/runner.py)
# Límite global de fuentes en paralelo. El documento (4.6) pide también un
# límite por dominio: aún no está implementado.
MAX_FUENTES_CONCURRENTES = 5

# Colector web (usado por core/ingestion/web_collector.py)
# Valores iniciales propuestos por mí; el documento v2 no fija estos números.
MAX_ARTICULOS_POR_EJECUCION = 30
PAUSA_ENTRE_ARTICULOS_SEGUNDOS = 1.0
MIN_CARACTERES_TEXTO_COMPLETO = 500  # por debajo, calidad_extraccion = "parcial"

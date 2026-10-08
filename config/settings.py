# config/settings.py
# Parámetros del proyecto. NO colocar secretos aquí (claves NVD, GitHub):
# esas van en variables de entorno (documento v2, sección 4.9).

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

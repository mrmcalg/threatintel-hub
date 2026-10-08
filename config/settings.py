# config/settings.py
# Parámetros del proyecto. NO colocar secretos aquí (claves NVD, GitHub):
# esas van en variables de entorno (documento v2, sección 4.9).

# Normalización de URLs (usado por core/ingestion/normalizer.py)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
TRACKING_PREFIXES = ("utm_",)

# ThreatIntel Hub: módulo de ingesta (borrador)

Borrador de la **Fase 1 (Ingesta y recopilación)** del proyecto ThreatIntel Hub v2. Esta fase termina con una colección de ítems crudos normalizados y deduplicados, y con el estado de salud de cada fuente registrado.

> Estado: borrador en desarrollo. No es una versión usable todavía.

## Estado actual

| Componente | Archivo | Estado |
| :--- | :--- | :--- |
| Contrato del ítem crudo | `core/models/raw_item.py` | Hecho |
| Normalizador (URL canónica, fechas UTC, hashes) | `core/ingestion/normalizer.py` | Hecho, con pruebas |
| Parámetros del proyecto | `config/settings.py` | Hecho (solo parámetros de tracking) |
| Definición de fuentes | `config/sources.yaml` | Copiado del documento v2, sin validar |
| Validación de `sources.yaml` | `core/models/source_config.py` | Pendiente |
| Deduplicación y persistencia (SQLite) | `core/ingestion/dedup.py` | Pendiente |
| Cliente HTTP (timeouts, backoff, ETag, anti-SSRF) | `core/ingestion/http_client.py` | Pendiente |
| Colector RSS | `core/ingestion/rss_collector.py` | Pendiente |
| Colector API (CISA KEV) | `core/ingestion/api_collector.py` | Pendiente |
| Circuit breaker, scheduler, colector web | `health.py`, `scheduler.py`, `web_collector.py` | Pendiente (Sprint 1b) |

## Requisitos

- Python 3.11 o superior
- Dependencias en `requirements.txt` (`httpx`, `feedparser`, `defusedxml`, `pydantic`, `pyyaml`, `pytest`, `respx`)

## Instalación (Windows, PowerShell)

```powershell
git clone <URL-del-repositorio>
cd threatintel-hub
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Pruebas

```powershell
python -m pytest -v
```

Se usa `python -m pytest` para que la carpeta del proyecto quede en la ruta de importación.

## Estructura

```text
threatintel-hub/
├── config/
│   ├── settings.py        # Parámetros (SIN secretos)
│   └── sources.yaml       # Fuentes y enriquecedores
├── core/
│   ├── models/            # Modelos Pydantic (contrato del ítem crudo)
│   └── ingestion/         # Normalización y, más adelante, colectores
├── storage/               # Datos locales (ignorados por git)
├── tests/
│   ├── fixtures/          # Feeds de ejemplo para pruebas
│   └── test_normalizer.py
└── requirements.txt
```

## Reglas del proyecto

- **Sin URLs de fuentes en el código de producción.** Las fuentes se declaran en `config/sources.yaml`. Los tests sí pueden contener datos literales.
- **Sin secretos en el repositorio.** Las claves (por ejemplo `NVD_API_KEY`, `GITHUB_TOKEN`) se leen de variables de entorno.
- **Fechas en UTC.** Si una fuente no trae fecha de publicación, el campo queda `null`; no se inventa.

## Pendiente de verificar

- Las URLs de los feeds en `config/sources.yaml` no han sido comprobadas.
- Los parámetros de las APIs (NVD, EPSS, GitHub) deben revisarse en su documentación oficial antes de implementar.
- Decisiones abiertas del documento v2 (sección 10): precedencia YAML vs. BD, umbrales del circuit breaker, política legal de robots.txt.

## Documentación de referencia

Especificación completa: `proyecto_cti_threat_intel_hub_v2.md` (sección 4 para la ingesta).

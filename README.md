# ThreatIntel Hub: módulo de ingesta (borrador)

Borrador de la **Fase 1 (Ingesta y recopilación)** de ThreatIntel Hub. Descarga fuentes de ciberseguridad (feeds RSS y el catálogo CISA KEV), las normaliza al contrato de ítem crudo, elimina duplicados y registra el estado de cada fuente en SQLite.

> Estado: borrador funcional de la ingesta. El resto del pipeline (enriquecimiento, decisión, reportes y consola web) pertenece a otros módulos y no está aquí.

## Qué hace hoy

- Lee y **valida** `config/sources.yaml` con un esquema (rechaza campos desconocidos, tipos inválidos, URLs no http/https, ids duplicados).
- Descarga cada fuente activa con un cliente HTTP con timeout, límite de tamaño, reintentos con backoff y jitter, `Retry-After`, peticiones condicionales (ETag / Last-Modified) y protección anti-SSRF.
- Convierte cada entrada en un `RawItem` (contrato definido en `core/models/raw_item.py`).
- **Deduplica** por `guid` + fuente, luego por URL canónica y luego por hash del contenido. Reejecutar no crea duplicados.
- Guarda ítems, ejecuciones y **estado por fuente** (ETag, último éxito, fallos consecutivos) en SQLite.
- Un fallo en una fuente se registra y **no detiene a las demás**.

## Estado de los componentes

| Componente | Archivo | Estado |
| :--- | :--- | :--- |
| Contrato del ítem crudo | `core/models/raw_item.py` | Hecho |
| Validación de `sources.yaml` | `core/models/source_config.py` | Hecho |
| Normalizador (URL canónica, fechas UTC, hashes) | `core/ingestion/normalizer.py` | Hecho |
| Deduplicación, ejecuciones y estado por fuente | `core/ingestion/dedup.py` | Hecho |
| Cliente HTTP con anti-SSRF | `core/ingestion/http_client.py` | Hecho (sin `robots.txt`) |
| Interfaz de colectores | `core/ingestion/base.py` | Hecho |
| Colector RSS / Atom | `core/ingestion/rss_collector.py` | Hecho |
| Colector CISA KEV (tipo `api`) | `core/ingestion/api_collector.py` | Hecho (solo formato KEV) |
| Orquestador | `core/ingestion/runner.py` | Hecho |
| Circuit breaker y feed silencioso | `health.py` | Pendiente |
| Planificador por `frecuencia_minutos` | `scheduler.py` | Pendiente |
| Sincronización YAML <-> BD (override `activo`) | `config_sync.py` | Pendiente |
| Colector web (Trafilatura / Playwright) | `web_collector.py` | Pendiente |

## Fuentes activas probadas con datos reales (2026-10-08)

| Fuente | Tipo | Resultado de la primera ejecución |
| :--- | :--- | :--- |
| `cisa_kev_api` | api | 1739 ítems |
| `zdi_0day_feed` | rss | 20 ítems |
| `project_zero_rss` | rss | 7 ítems |
| `bleeping_computer_news` | rss | 15 ítems |
| `the_hacker_news` | rss | 50 ítems |

Nota sobre `project_zero_rss`: el feed completo pesa unos 11 MB y supera el límite de 5 MB por respuesta, por eso la URL usa el endpoint de resúmenes (`posts/summary`).

## Requisitos

- Python 3.11 o superior
- Dependencias en `requirements.txt`

## Instalación (Windows, PowerShell)

```powershell
git clone <URL-del-repositorio>
cd threatintel-hub
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Uso

Ejecutar la ingesta (todas las fuentes activas) desde la raíz del proyecto:

```powershell
python -m core.ingestion.runner
```

Ejecutar solo algunas fuentes:

```powershell
python -m core.ingestion.runner --solo cisa_kev_api bleeping_computer_news
```

La base de datos queda en `storage/database.sqlite` (excluida de git). Ver el estado guardado de cada fuente:

```powershell
python -c "import sqlite3; c=sqlite3.connect('storage/database.sqlite'); [print(r) for r in c.execute('select fuente_id, ultimo_exito, fallos_consecutivos, ultimo_error from estado_fuentes')]"
```

Códigos de salida del runner: `0` sin errores, `1` si alguna fuente falló, `2` si la configuración o los argumentos son inválidos.

## Pruebas

```powershell
python -m pytest -v
```

Se usa `python -m pytest` para que la raíz del proyecto quede en la ruta de importación. A 2026-10-08 la suite tenía 96 pruebas pasando.

Las pruebas con feeds reales se activan solo si existen archivos en `tests/fixtures/` (están excluidos de git; ver más abajo):

```powershell
Invoke-WebRequest -Uri "<URL del feed>" -OutFile tests\fixtures\nombre.xml -UserAgent "ThreatIntelHub/2.0"
Invoke-WebRequest -Uri "<URL del catálogo KEV>" -OutFile tests\fixtures\known_exploited_vulnerabilities.json -UserAgent "ThreatIntelHub/2.0"
```

## Estructura

```text
threatintel-hub/
├── config/
│   ├── settings.py        # Parámetros y rutas (SIN secretos)
│   └── sources.yaml       # Fuentes y enriquecedores
├── core/
│   ├── models/            # raw_item.py, source_config.py
│   └── ingestion/         # normalizer, dedup, http_client, base,
│                          # rss_collector, api_collector, runner
├── storage/               # Datos locales (ignorados por git)
├── tests/
│   ├── fixtures/          # Feeds reales descargados (ignorados por git)
│   └── test_*.py
└── requirements.txt
```

## Reglas del proyecto

- **Sin URLs de fuentes en el código de producción.** Las fuentes se declaran en `config/sources.yaml`. Los tests sí pueden contener datos literales.
- **Sin secretos en el repositorio.** Las claves futuras (`NVD_API_KEY`, `GITHUB_TOKEN`) se leerán de variables de entorno.
- **Fechas en UTC.** Si una fuente no trae fecha de publicación, el campo queda `null`; no se inventa.
- **Contenido de terceros fuera de git.** `tests/fixtures/*.xml`, `tests/fixtures/*.json` y `storage/` están en `.gitignore` hasta resolver la decisión legal pendiente.

## Decisiones de diseño tomadas en este borrador

Propuestas del borrador que el documento de referencia no fija y que el equipo debe validar:

1. La deduplicación se hace dentro de cada fuente; agrupar la misma noticia entre fuentes es trabajo de la Fase 2.
2. Un `guid` repetido con texto distinto se marca `actualizado`.
3. Una entrada RSS sin texto calcula su hash sobre título + URL canónica, para no colapsar entradas vacías entre sí.
4. Las entradas KEV usan la ficha del CVE en NVD como URL de referencia, y `dateAdded` (00:00 UTC) como `fecha_publicacion`.
5. Un `Retry-After` mayor a `BACKOFF_MAX_SEGUNDOS` hace fallar la petición en lugar de esperar.
6. El campo `metodo_extraccion` incluye el valor `api_json`, que no figura en el documento original.
7. Los valores numéricos de `config/settings.py` (backoff, redirecciones, concurrencia) son puntos de partida.

## Limitaciones conocidas

- `respetar_robots_txt: true` está en `sources.yaml`, pero el cliente **aún no consulta `robots.txt`**.
- No hay límite de concurrencia por dominio, solo global.
- Entre la validación DNS y la conexión real existe una ventana de "DNS rebinding" sin cerrar.
- Si hay un proxy configurado por variable de entorno, el destino final lo resuelve el proxy y no la validación anti-SSRF.
- El runner ejecuta todas las fuentes activas cada vez que se invoca: aún no respeta `frecuencia_minutos`.
- No hay circuit breaker: se cuentan los fallos consecutivos, pero ninguna fuente se bloquea.
- Toda fuente de tipo `api` se interpreta con el formato de CISA KEV.
- Las capturas crudas (`raw_ref`, `storage/raw_inputs/`) no se guardan todavía.

## Pendiente de resolver (del documento de referencia)

- Precedencia YAML vs. base de datos y flujo de fuentes creadas desde la consola.
- Umbrales del circuit breaker y del "feed silencioso".
- Política legal: `robots.txt`, almacenamiento y republicación de contenido de terceros.
- Revisar la URL de cada fuente antes de activarla en `sources.yaml`.

## Documento de referencia

Especificación: sección 4 del documento de ThreatIntel Hub v2. El archivo `proyecto_cti_threat_intel_hub.md` incluido en este repositorio es la versión original (v1) y conviene reemplazarlo por la v2.

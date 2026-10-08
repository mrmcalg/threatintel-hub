# ThreatIntel Hub (v2): Sistema Autónomo de CTI, Triaje de Explotación y Reportes Bimodales

> **Versión 2.** Revisión del documento original. La sección 11 lista cada cambio respecto a v1 y su motivo.
> Las decisiones marcadas como **[DECISIÓN PENDIENTE]** deben ser validadas por el equipo antes de implementarse.

## 1. Visión y Resumen Ejecutivo del Proyecto

El objetivo es construir un **Centro de Inteligencia de Amenazas (Cyber Threat Intelligence - CTI)** autónomo, modular y desacoplado, que reduzca el ruido de los agregadores de ciberseguridad tradicionales.

El sistema implementa un pipeline de tres etapas (**Ingesta Multifuente -> Enriquecimiento y Decisión Fáctica -> Salida Bimodal en JSON y PDF**), operado desde una **Consola de Administración Web** para que analistas del SOC (Nivel 1 y Nivel 2) puedan configurarlo, auditarlo y operarlo sin programar.

### Alcance por responsable

| Módulo | Responsable | Estado |
| :--- | :--- | :--- |
| Ingesta y recopilación (Capa 3, Fase 1) | **Asignado** | Especificación detallada en la sección 4 |
| Enriquecimiento y motor de decisión (Fase 2) | Por asignar | Especificación de alto nivel |
| Reportes PDF (Fase 3) | Por asignar | Especificación de alto nivel |
| Consola web y API | Por asignar | Especificación de alto nivel |

---

## 2. Diagnóstico del Estado del Arte y Vacíos de la Industria

Al analizar proyectos de referencia en GitHub (*AuvaLabs/threatwatch*, *Ethan-Andrews/ThreatIntel-Aggregator*, *fukusuket/ThreatfeedCollector*, *OpenCTI Connectors*, *brunosergi/ai-threat-intel-bulletin*), se identificaron cinco carencias que este proyecto busca mitigar.

| Área Crítica | Problema en Proyectos Actuales | Mitigación en Este Proyecto |
| :--- | :--- | :--- |
| **Gestión de Fuentes** | Fuentes *hardcodeadas*; si cambia el HTML o cae una URL, el colector falla en silencio. | Fuentes declaradas en `sources.yaml`, extracción de texto principal sin selectores CSS frágiles y *circuit breaker* con estado de salud visible. |
| **Ruido y Falsos Zero-Days** | Etiquetan "Zero-Day" por la sola presencia de la palabra en el titular. | Clasificación basada en evidencia: CISA KEV, EPSS, PoC público y **estado de parche**. Se separa "explotado activamente" de "zero-day" (sección 5.3). |
| **Duplicación Temporal** | N artículos sobre el mismo CVE generan N registros. | Clustering de incidentes y *timeline diff*. |
| **Calidad de los IoCs** | Regex ciego que extrae dominios legítimos y rangos reservados. | Sanitización contra listas de exclusión (rangos reservados, hashes triviales, dominios legítimos) y *defanging* solo en presentación. |
| **Entregables y Adopción** | Volcados planos sin distinguir audiencias. | Salida bimodal (PDF Ejecutivo + Técnico) y consola web. |

> Nota: estas mitigaciones son objetivos de diseño. Su efectividad real debe medirse con un conjunto de prueba (sección 9).

---

## 3. Arquitectura Global del Sistema

```text
┌────────────────────────────────────────────────────────────────────────┐
│                   CAPA 1: CONSOLA WEB (INTERFAZ SOC)                   │
│  [Dashboard de Alertas]  [Gestor de Fuentes]  [Generador de PDFs]     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (REST API autenticada)
┌───────────────────────────────────▼────────────────────────────────────┐
│                    CAPA 2: BACKEND Y ORQUESTACIÓN                      │
│             FastAPI (API REST) + Scheduler (proceso único)             │
│       Endpoints: /fuentes, /ejecutar-pipeline, /incidentes, /pdf       │
└───────────────────┬────────────────────────────────┬───────────────────┘
                    │                                │
┌───────────────────▼────────────────┐   ┌───────────▼───────────────────┐
│     CAPA 3: PIPELINE DE CTI        │   │     CAPA 4: ALMACENAMIENTO    │
│  1. INGESTA (feeds, APIs, web)     │   │  • Base de datos (SQLite/PG)  │
│  2. ENRIQUECIMIENTO (KEV, EPSS,    │   │    - Estado operativo fuentes │
│     NVD, GitHub PoC)               │   │    - Ítems crudos             │
│  3. Motor de Decisión determinista │   │    - Incidentes y línea de    │
│  4. Sanitizador de IoCs            │   │      tiempo                   │
│  5. Generador PDF Bimodal          │   │  • Archivos JSON/PDF generados│
└────────────────────────────────────┘   └───────────────────────────────┘
```

---

## 4. Fase 1: Ingesta y Recopilación (alcance asignado)

Esta fase termina cuando existe una colección de **ítems crudos normalizados y deduplicados**, con el estado de salud de cada fuente registrado. Todo lo posterior (extracción de entidades, decisión, reporte) consume ese contrato.

### 4.1 Separación conceptual: Fuentes vs. Enriquecedores

v1 mezclaba ambos roles. Se separan porque tienen ciclos de vida distintos.

| Concepto | Qué hace | Ejemplos | Cuándo se ejecuta |
| :--- | :--- | :--- | :--- |
| **Fuente (source)** | Produce ítems de contenido nuevo (artículos, avisos, entradas de catálogo). | RSS de BleepingComputer, blog de Project Zero, Cisco Talos (web) | Periódico, según `frecuencia_minutos` |
| **Enriquecedor (enricher)** | Dado un CVE ya detectado, devuelve datos adicionales. | EPSS, NVD (CVSS, CPE, parche), GitHub PoC | Bajo demanda por CVE, con caché |

CISA KEV cumple **ambos roles**: se ingesta como fuente (detectar CVEs nuevos añadidos al catálogo) y se usa como enriquecedor (¿este CVE está en KEV?). Se modela como una sola descarga del catálogo, cacheada localmente, que sirve a los dos usos.

### 4.2 Fuente de verdad de la configuración

**[DECISIÓN PENDIENTE]** Propuesta para eliminar la ambigüedad de v1:

- **`sources.yaml`**: definición declarativa y versionable (id, url, tipo, categoría, frecuencia, parámetros). Es la fuente de verdad de *qué existe*.
- **Base de datos**: estado operativo (último éxito, fallos consecutivos, estado del circuit breaker, ETag/Last-Modified, y el override `activo` puesto desde la consola).
- **Regla de precedencia**: el valor `activo` de la BD, si existe, prevalece sobre el del YAML. Al arrancar y al recargar, el YAML se sincroniza con la BD (altas, bajas, cambios de URL). Las fuentes creadas desde la consola se exportan al YAML o se marcan `origen: consola`.

### 4.3 `sources.yaml` (v2)

```yaml
# config/sources.yaml
version: 2

defaults:
  timeout_segundos: 20
  max_bytes: 5242880          # 5 MB por respuesta
  reintentos: 3
  user_agent: "ThreatIntelHub/2.0 (+contacto-soc@ejemplo.org)"
  respetar_robots_txt: true

fuentes:
  - id: "cisa_kev_api"
    nombre: "CISA Known Exploited Vulnerabilities"
    tipo: "api"
    url: "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    activo: true
    categoria: "oficial_gobierno"
    frecuencia_minutos: 60
    rol: ["fuente", "enriquecedor"]
    fiabilidad: "alta"

  - id: "zdi_0day_feed"
    nombre: "Zero Day Initiative Blog"
    tipo: "rss"
    url: "https://www.zerodayinitiative.com/blog?format=rss"
    activo: true
    categoria: "investigacion_0day"
    frecuencia_minutos: 30
    fiabilidad: "alta"

  - id: "project_zero_rss"
    nombre: "Google Project Zero"
    tipo: "rss"
    url: "https://googleprojectzero.blogspot.com/feeds/posts/default?alt=rss"
    activo: true
    categoria: "investigacion_0day"
    frecuencia_minutos: 60
    fiabilidad: "alta"

  - id: "bleeping_computer_news"
    nombre: "BleepingComputer News"
    tipo: "rss"
    url: "https://www.bleepingcomputer.com/feed/"
    activo: true
    categoria: "noticias_soc"
    frecuencia_minutos: 30
    fiabilidad: "media"

  - id: "the_hacker_news"
    nombre: "The Hacker News"
    tipo: "rss"
    url: "https://feeds.feedburner.com/TheHackersNews"
    activo: true
    categoria: "noticias_soc"
    frecuencia_minutos: 30
    fiabilidad: "media"

  # Ejemplo de fuente web (sin feed). Las URLs concretas deben validarse
  # antes de activar: v1 nombraba Talos y Mandiant pero no incluía ninguna entrada.
  - id: "ejemplo_web_advisory"
    nombre: "Ejemplo: portal de avisos sin RSS"
    tipo: "web"
    url: "https://ejemplo.org/advisories/"
    activo: false
    categoria: "avisos_fabricante"
    frecuencia_minutos: 120
    fiabilidad: "alta"
    web:
      modo: "listado"            # listado -> enlaces a artículos -> extracción
      patron_enlaces: "/advisories/[a-z0-9-]+"
      render_js: false           # true requiere Playwright

enriquecedores:
  - id: "epss"
    nombre: "FIRST EPSS"
    url_api: "https://api.first.org/data/v1/epss"
    modo: "api_por_cve"          # alternativa: descarga masiva diaria
    cache_horas: 24
    activo: true

  - id: "nvd"
    nombre: "NIST NVD 2.0"
    url_api: "https://services.nvd.nist.gov/rest/json/cves/2.0"
    cache_horas: 24
    requiere_api_key: true       # clave por variable de entorno NVD_API_KEY
    activo: true

  - id: "github_poc"
    nombre: "GitHub PoC search"
    url_api: "https://api.github.com/search/repositories"
    cache_horas: 12
    requiere_api_key: true       # token por variable de entorno GITHUB_TOKEN
    activo: true
```

> Los parámetros exactos de las APIs (rutas, límites de peticiones, formato de respuesta) deben verificarse en su documentación oficial antes de implementar.

### 4.4 Contrato del ítem crudo (salida de todos los colectores)

Todo colector, sin importar su tipo, produce objetos con este esquema. Es el único punto de acoplamiento entre ingesta y procesamiento.

```json
{
  "item_id": "sha256 de (fuente_id + url_canonica)",
  "fuente_id": "the_hacker_news",
  "tipo_fuente": "rss",
  "url_original": "https://ejemplo.com/articulo?utm_source=x",
  "url_canonica": "https://ejemplo.com/articulo",
  "guid": "identificador del feed, si existe",
  "titulo": "Texto del título",
  "autor": null,
  "fecha_publicacion": "2026-10-06T14:30:00Z",
  "fecha_captura": "2026-10-06T14:45:12Z",
  "idioma": "en",
  "texto": "Texto principal extraído",
  "hash_contenido": "sha256 del texto normalizado",
  "metodo_extraccion": "feed_summary | feed_content | trafilatura | playwright+trafilatura",
  "calidad_extraccion": "completa | parcial | solo_resumen",
  "estado": "nuevo | actualizado | duplicado",
  "raw_ref": "storage/raw_inputs/2026-10-06/the_hacker_news/<item_id>.json"
}
```

Reglas:
- `fecha_publicacion` se normaliza a UTC. Si la fuente no la trae, queda `null` (no se inventa).
- `calidad_extraccion` permite a la fase 2 decidir si un ítem con solo resumen basta para extraer entidades.
- `estado = actualizado` cuando la `url_canonica` ya existe pero `hash_contenido` cambió.

### 4.5 Deduplicación a nivel de ingesta

Orden de comprobación (primera coincidencia gana):
1. `guid` + `fuente_id`.
2. `url_canonica` (sin parámetros de tracking, sin fragmento, esquema y host en minúsculas).
3. `hash_contenido`, para detectar el mismo texto publicado en URLs distintas.

Esto es **deduplicación de ítems**. El clustering por incidente/CVE (agrupar artículos distintos sobre el mismo fallo) pertenece a la fase 2 (`deduplicator.py`) y no es responsabilidad de la ingesta.

### 4.6 Robustez de la ingesta

| Aspecto | Requisito |
| :--- | :--- |
| **Peticiones condicionales** | Enviar `If-None-Match` / `If-Modified-Since` con los valores guardados; tratar `304` como éxito sin ítems nuevos. |
| **Reintentos** | Backoff exponencial con jitter; respetar `Retry-After` en `429`/`503`. |
| **Idempotencia** | Reejecutar una ingesta no debe crear ítems duplicados. |
| **Límites** | Timeout y tamaño máximo de respuesta por fuente (ver `defaults`). |
| **robots.txt y términos de uso** | Verificar antes de scrapear fuentes `web`. Revisión legal de almacenamiento de contenido de terceros **[DECISIÓN PENDIENTE]**. |
| **Concurrencia** | Límite de concurrencia global y por dominio. |
| **Aislamiento de fallos** | Un fallo en una fuente nunca detiene a las demás. |

### 4.7 Circuit breaker y salud de fuentes

Estados propuestos **[DECISIÓN PENDIENTE: umbrales]**:

| Estado | Condición de entrada | Comportamiento |
| :--- | :--- | :--- |
| `ACTIVA` | Última ejecución exitosa | Ejecución normal |
| `DEGRADADA` | 1 a 2 fallos consecutivos, o respuesta correcta con 0 ítems durante N ciclos | Ejecución normal, advertencia visible |
| `ABIERTA` (bloqueada) | 3 o más fallos consecutivos | No se consulta; reintento tras enfriamiento creciente |
| `SEMI-ABIERTA` | Fin del enfriamiento | Una petición de prueba; si funciona, vuelve a `ACTIVA` |
| `PAUSADA` | Desactivada manualmente | No se ejecuta |

Cada ejecución registra: fuente, inicio, duración, código HTTP, ítems nuevos/actualizados/duplicados, error. Alerta específica de **"feed silencioso"**: respuesta `200` con estructura válida pero sin ítems nuevos durante un periodo superior a 3 veces su frecuencia habitual.

### 4.8 Estrategia para fuentes sin feed (tipo `web`)

1. Descargar la página de listado y extraer enlaces según `patron_enlaces`.
2. Descargar cada artículo no visto y extraer el texto principal con **Trafilatura** (heurística tipo *readability*), sin selectores CSS por sitio.
3. Si `render_js: true`, renderizar con **Playwright** antes de extraer.
4. Marcar `calidad_extraccion = parcial` cuando el texto extraído sea anormalmente corto.

Limitaciones conocidas (no se "resuelven de raíz"): contenido tras JavaScript, paywalls, bloqueo anti-bot (por ejemplo CAPTCHA) y cambios drásticos de estructura de listado. Para estos casos la fuente pasa a `DEGRADADA`/`ABIERTA` en lugar de fallar en silencio.

### 4.9 Seguridad de la ingesta

- **Anti-SSRF** (aplica a "Probar Conexión" y a cualquier URL configurable): resolver DNS y rechazar IPs privadas, loopback, link-local y metadatos cloud; limitar esquemas a `http/https`; revalidar en cada redirección; limitar número de redirecciones.
- **Parseo XML seguro** (`defusedxml` o equivalente) para evitar XXE y expansión de entidades.
- **Límite de tamaño y de tiempo** por respuesta.
- **Secretos** (claves NVD, GitHub): solo por variables de entorno, nunca en `settings.py` ni en el YAML.
- **El contenido ingestado es entrada no confiable**: se almacena como datos; nunca se interpreta como instrucciones (relevante si la fase 2 usa un LLM).

### 4.10 Criterios de aceptación de la fase 1

- [ ] `sources.yaml` se valida al arrancar con un esquema (campos obligatorios, tipos, URLs válidas) y rechaza configuraciones inválidas con mensaje claro.
- [ ] Agregar, pausar o eliminar una fuente no requiere reiniciar el servicio.
- [ ] Una fuente caída no impide que las demás se ingesten.
- [ ] Reejecutar la ingesta sobre los mismos datos produce cero ítems duplicados.
- [ ] Cada ítem cumple el contrato de la sección 4.4.
- [ ] La salud de cada fuente es consultable (estado, último éxito, último error).
- [ ] Hay pruebas con fixtures de feeds reales (bien formados, malformados, vacíos, con codificación atípica).
- [ ] "Probar Conexión" rechaza destinos internos (prueba de SSRF).

---

## 5. Fase 2: Enriquecimiento, Decisión y Formato JSON (alto nivel)

### 5.1 Extracción y normalización de entidades
- Detección de CVE (`CVE-\d{4}-\d{4,7}`), normalizada a mayúsculas.
- Extracción de IoCs en forma **cruda** (sin defang) y validación sintáctica (IPv4/IPv6, dominio, URL, hash por longitud).
- Sanitización (`ioc_sanitizer.py`), descartando:
  - Rangos reservados: RFC 1918, loopback, link-local, CGNAT (100.64.0.0/10) y **rangos de documentación RFC 5737** (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24).
  - Hashes triviales conocidos (por ejemplo, el SHA256 de un archivo vacío).
  - Dominios de infraestructura legítima citados como contexto.
- **Advertencia sobre listas Top 1M (Tranco/Umbrella):** incluyen servicios que los atacantes también abusan (almacenamiento, paste, repositorios). Se usan para *reducir confianza* del IoC, no para descartarlo automáticamente **[DECISIÓN PENDIENTE]**.
- El *defanging* se aplica **solo al renderizar para humanos**. El almacenamiento interno y las exportaciones para SIEM/EDR usan valores crudos.

### 5.2 Enriquecimiento
- **CISA KEV:** pertenencia, fecha de adición, fecha límite de remediación (`dueDate`).
- **EPSS:** probabilidad y percentil.
- **NVD:** CVSS, CPE/productos afectados, referencias, **información de parche/solución cuando exista**.
- **GitHub PoC:** repositorios candidatos. Un repositorio encontrado es un *candidato*, no un "PoC funcional verificado"; se registra con `poc_verificado: false` salvo revisión humana.

### 5.3 Motor de decisión determinista

Se separan dos preguntas distintas que v1 mezclaba:

1. **¿Cuán urgente es?** (`nivel_amenaza`).
2. **¿Es un zero-day?** (`zero_day`), que requiere evidencia de explotación **anterior a la disponibilidad de parche**.

**Entradas:** `en_kev`, `epss`, `poc_candidato`, `poc_verificado`, `parche_disponible` (`si | no | desconocido`), `fecha_parche`, `fecha_primera_explotacion_conocida`.

**Reglas de `nivel_amenaza`** (evaluadas en orden, la primera que coincide gana; todas las ramas están cubiertas):

| Orden | Condición | Nivel |
| :---: | :--- | :--- |
| 1 | `en_kev = true` | `CRITICO_EXPLOTADO_ACTIVO` |
| 2 | `epss > 0.60` Y `poc_candidato = true` | `CRITICO_EXPLOTADO_ACTIVO` si `poc_verificado`, si no `ALTO_EMERGENTE` |
| 3 | `poc_candidato = true` O `epss >= 0.20` | `ALTO_EMERGENTE` |
| 4 | `epss >= 0.05` O CVE mencionado por fuente de fiabilidad alta | `MEDIO_MONITOREO` |
| 5 | Cualquier otro caso | `BAJO_INFORMATIVO` |

> Los umbrales (0.60, 0.20, 0.05) son parámetros de `settings.py`, no constantes de código. **[DECISIÓN PENDIENTE]** validarlos con datos históricos.

**Regla de `zero_day`:**

| Valor | Condición |
| :--- | :--- |
| `CONFIRMADO` | Hay evidencia de explotación con fecha anterior a `fecha_parche`, o parche inexistente a la fecha de la primera explotación documentada por un fabricante o CISA. |
| `PROBABLE` | `en_kev = true` y `parche_disponible = no` |
| `NO_DETERMINADO` | Falta información de parche o de fechas |
| `NO` | Parche disponible antes de la primera explotación conocida |

Estar en KEV **no** implica zero-day. Un CVE en KEV con parche disponible es "explotado activamente", no zero-day.

### 5.4 Schema JSON de salida (v2)

```json
{
  "schema_version": "2.0",
  "id_incidente": "INC-2026-0042",
  "tipo": "vulnerabilidad",
  "cves": ["CVE-2026-35273"],
  "titulo": "Ejecución Remota de Código no Autenticada en Pasarelas Enterprise",
  "fecha_primera_deteccion": "2026-10-06T14:30:00Z",
  "fecha_ultima_actualizacion": "2026-10-08T09:15:00Z",
  "clasificacion_decision": {
    "nivel_amenaza": "CRITICO_EXPLOTADO_ACTIVO",
    "zero_day": "PROBABLE",
    "en_cisa_kev": true,
    "fecha_adicion_kev": "2026-10-08T02:00:00Z",
    "epss_score": 0.884,
    "epss_percentil": 99.2,
    "epss_fecha_consulta": "2026-10-08T09:00:00Z",
    "cvss": { "version": "3.1", "vector": null, "score": null },
    "poc_candidato": true,
    "poc_verificado": false,
    "fuente_poc": "https://github.com/ejemplo/poc-cve-2026-35273",
    "parche_disponible": "no",
    "fecha_parche": null,
    "reglas_aplicadas": ["R1_KEV"]
  },
  "tecnologias_afectadas": [
    { "proveedor": "Enterprise Vendor", "producto": "Gateway OS", "versiones": "<= 12.4.1", "cpe": null }
  ],
  "resumen_ejecutivo": "Texto generado y marcado como borrador hasta revisión.",
  "resumen_origen": "plantilla | llm | analista",
  "evolucion_linea_tiempo": [
    { "timestamp": "2026-10-06T14:30:00Z", "evento": "Aviso preliminar en medio especializado.", "item_id": "..." },
    { "timestamp": "2026-10-07T08:00:00Z", "evento": "Aparece repositorio candidato de PoC en GitHub.", "item_id": "..." },
    { "timestamp": "2026-10-08T02:00:00Z", "evento": "CISA añade el CVE al catálogo KEV.", "item_id": "..." }
  ],
  "iocs": [
    {
      "tipo": "ipv4",
      "valor": "198.51.100.45",
      "estado_verificacion": "extraido_sin_verificar",
      "confianza": "baja",
      "fuente_item_id": "..."
    }
  ],
  "acciones_mitigacion": [
    { "accion": "Aislar la interfaz de administración de Internet.", "origen": "item_id o 'plantilla'" }
  ],
  "fuentes_referencia": [
    "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
  ]
}
```

Cambios clave frente a v1: `cves` es lista (varios CVE o ninguno), `zero_day` separado de `nivel_amenaza`, campos de parche y de fecha de consulta, IoCs crudos con `estado_verificacion` (en lugar de `iocs_verificados` sin mecanismo de verificación), `reglas_aplicadas` para auditar la decisión, y trazabilidad por `item_id`.

Los IPs de ejemplo pertenecen a rangos de documentación y el sanitizador de 5.1 los descartaría; son solo ilustrativos.

### 5.5 Generación de contenido derivado
`resumen_ejecutivo`, `acciones_mitigacion` y `tecnologias_afectadas` requieren interpretar texto libre. **[DECISIÓN PENDIENTE]** definir el mecanismo (plantillas + NLP, o LLM con salida estructurada validada). Requisitos mínimos: marcar el origen en cada campo, traducción explícita (fuentes en inglés, reportes en español) y revisión de analista antes de distribuir.

---

## 6. Fase 3: Reporte Bimodal en PDF (alto nivel)

El JSON alimenta un generador (`reportlab` / `weasyprint`):

1. **Página 1: Resumen Ejecutivo (CISO y Gerencia de TI)**
   * Semáforo de criticidad según `nivel_amenaza`.
   * Estado de explotación (KEV, EPSS con fecha de consulta) y `zero_day` con su nivel de certeza.
   * Matriz de impacto: "¿Tenemos esta tecnología?". **[Requiere inventario de activos; no está en el alcance actual.]**
   * Decisión requerida.
2. **Páginas 2+: Runbook Operativo (SOC / Threat Hunters)**
   * Ficha técnica (CVSS, servicios involucrados).
   * Línea de tiempo.
   * Tabla de IoCs: columna *defanged* para lectura y exportación CSV/STIX en formato crudo para SIEM/EDR.
   * Reglas de detección sugeridas (Sigma/YARA), marcadas como *borrador sin validar*.
   * Procedimiento de contención y mitigación.

---

## 7. Consola de Administración Web (alto nivel)

### Vistas
1. **Live Triage Dashboard:** tarjetas por criticidad, filtros ("Explotados activamente", "Zero-day confirmado/probable", "Nuevos hoy", "Con PoC candidato").
2. **Source Manager:**
   * Alta de feeds RSS, APIs o sitios web con validación de esquema.
   * **"Probar Conexión"** con protección anti-SSRF (sección 4.9).
   * Switches ON/OFF (escriben el override en BD, sección 4.2).
   * Monitor de salud con los estados de la sección 4.7 y el motivo del último fallo.
3. **Visor de incidentes y cronología.**
4. **Generador de reportes de 1 clic.**

### Seguridad de la consola
- Autenticación obligatoria y roles (por ejemplo `lector`, `analista`, `admin`). Solo `admin` modifica fuentes.
- Registro de auditoría de cambios de configuración.
- Protección CSRF si se usa interfaz con cookies.

### Stack recomendado
* **Backend:** Python 3.11+ con FastAPI.
* **Frontend:** Streamlit (prototipo) o FastAPI + Jinja2/HTMX (producción ligera).
* **Base de datos:** SQLite (local) o PostgreSQL (múltiples analistas).
* **Planificador:** `APScheduler` en **un único proceso** de ingesta (con bloqueo/lock) para evitar ejecuciones duplicadas si la API corre con varios workers.
* **Cliente HTTP:** `httpx` (asíncrono), `feedparser`, `trafilatura`, `defusedxml`.
* **Validación:** Pydantic.
* **Pruebas:** `pytest`, `respx` o VCR.

---

## 8. Estructura del Repositorio Recomendada

```text
threatintel-hub/
│
├── config/
│   ├── sources.yaml              # Fuentes y enriquecedores (v2)
│   ├── settings.py               # Umbrales y parámetros (SIN secretos)
│   └── exclusions/               # Listas de exclusión para IoCs
│       ├── reserved_ranges.txt   # RFC 1918, 5737, loopback, CGNAT...
│       ├── trivial_hashes.txt    # Hashes de archivo vacío, etc.
│       └── legit_domains.txt     # Dominios legítimos / baja confianza
│
├── core/
│   ├── models/                   # Modelos Pydantic compartidos
│   │   ├── raw_item.py           # Contrato del ítem crudo (4.4)
│   │   └── source_config.py      # Validación de sources.yaml
│   │
│   ├── ingestion/                # FASE 1 (alcance asignado)
│   │   ├── base.py               # BaseCollector (interfaz abstracta)
│   │   ├── http_client.py        # Cliente con timeouts, backoff, ETag, anti-SSRF
│   │   ├── rss_collector.py
│   │   ├── api_collector.py      # APIs JSON genéricas (p. ej. CISA KEV)
│   │   ├── web_collector.py      # Listado + extracción (Trafilatura/Playwright)
│   │   ├── normalizer.py         # URL canónica, fechas UTC, hashes
│   │   ├── dedup.py              # Deduplicación de ítems (4.5)
│   │   ├── health.py             # Circuit breaker y estado de fuentes (4.7)
│   │   ├── scheduler.py          # Planificación (proceso único)
│   │   └── config_sync.py        # Sincronización YAML <-> BD (4.2)
│   │
│   ├── enrichers/                # Enriquecimiento (Fase 2)
│   │   ├── base.py
│   │   ├── kev.py
│   │   ├── epss.py
│   │   ├── nvd.py
│   │   └── github_poc.py
│   │
│   ├── engine/                   # Procesamiento y decisión (Fase 2)
│   │   ├── entity_extractor.py
│   │   ├── ioc_sanitizer.py
│   │   ├── decision_engine.py    # Reglas de 5.3
│   │   ├── incident_clustering.py# Agrupación por CVE/campaña + timeline diff
│   │   └── summarizer.py         # Resumen/mitigaciones/traducción (5.5)
│   │
│   └── reporting/                # Salidas (Fase 3)
│       ├── json_builder.py
│       └── pdf_generator.py
│
├── web/
│   ├── app.py
│   ├── auth.py                   # Autenticación y roles
│   ├── routes/
│   └── templates/
│
├── storage/
│   ├── database.sqlite
│   ├── raw_inputs/               # Capturas crudas por fecha y fuente
│   └── generated_reports/
│
├── tests/
│   ├── fixtures/                 # Feeds reales: válidos, malformados, vacíos
│   ├── test_ingestion/
│   ├── test_security/            # SSRF, XXE, límites de tamaño
│   └── test_engine/
│
├── requirements.txt
└── README.md
```

---

## 9. Plan de Implementación por Fases

1. **Sprint 1 (Ingesta, núcleo):**
   - Modelos Pydantic: `source_config.py` y `raw_item.py`.
   - `http_client.py` (timeouts, backoff, ETag, anti-SSRF, límite de tamaño).
   - `BaseCollector`, `rss_collector.py` y `api_collector.py` (CISA KEV).
   - `normalizer.py` y `dedup.py`.
   - Persistencia de ítems crudos y estado de ejecución.
   - Pruebas con fixtures de feeds reales.
2. **Sprint 1b (Ingesta, resiliencia y web):**
   - `health.py` (circuit breaker), `config_sync.py`, `scheduler.py`.
   - `web_collector.py` (`semantic_scraper` de v1, que no estaba asignado a ningún sprint).
   - Enriquecedores `kev.py` y `epss.py` si el equipo de fase 2 lo requiere.
3. **Sprint 2 (Motor de procesamiento):** entidades, sanitización, enriquecedores NVD/GitHub, `decision_engine.py`, clustering.
4. **Sprint 3 (Reportes):** JSON validado y PDF bimodal.
5. **Sprint 4 (Consola web):** autenticación, Source Manager, dashboard, descarga de reportes.

### Validación del sistema (transversal)
- Conjunto de prueba etiquetado con casos conocidos (CVE explotados con y sin parche, titulares clickbait, artículos con varios CVE) para medir falsos positivos de `zero_day` y calidad de IoCs.
- Métricas de ingesta: tasa de éxito por fuente, latencia, ítems duplicados evitados, "feeds silenciosos" detectados.

---

## 10. Decisiones Pendientes (resumen)

| # | Tema | Sección |
| :-: | :--- | :-: |
| 1 | Precedencia YAML vs. BD y flujo de fuentes creadas desde la consola | 4.2 |
| 2 | Umbrales del circuit breaker y del "feed silencioso" | 4.7 |
| 3 | Política legal: robots.txt, almacenamiento de contenido de terceros, republicación | 4.6 |
| 4 | Umbrales EPSS (0.60 / 0.20 / 0.05) | 5.3 |
| 5 | Uso de listas Top 1M: descartar o reducir confianza | 5.1 |
| 6 | Mecanismo de resumen y traducción (plantillas/NLP/LLM) | 5.5 |
| 7 | Inventario de activos para la matriz "¿tenemos esta tecnología?" | 6 |
| 8 | Qué significa "lógica estilo Jev AI" de v1 (no estaba definido; se sustituyó por reglas explícitas) | 5.3 |

---

## 11. Registro de Cambios respecto a v1

| # | Cambio | Motivo (hecho en v1) |
| :-: | :--- | :--- |
| 1 | Se separa `zero_day` de `nivel_amenaza`; nivel renombrado a `CRITICO_EXPLOTADO_ACTIVO` | v1 asignaba `CRITICO_ZERO_DAY` con solo estar en KEV, y el ejemplo mostraba un parche disponible. |
| 2 | Árbol de decisión completo con orden y nivel `BAJO_INFORMATIVO` | v1 dejaba sin cubrir EPSS > 0.60 sin PoC y fuera de KEV. |
| 3 | Campos `parche_disponible`, `fecha_parche`, `cvss`, `cpe`, `epss_fecha_consulta` | v1 usaba "sin parche oficial" en una regla sin campo que la soportara, y el PDF exigía CVSS sin fuente. |
| 4 | Fuentes vs. enriquecedores; `enriquecedores` en el YAML; NVD y GitHub añadidos | v1 tenía EPSS/NVD/GitHub fuera de `sources.yaml` y sin colector (NVD, GitHub). |
| 5 | Regla de precedencia YAML/BD | v1 declaraba dos fuentes de verdad. |
| 6 | Tipo `web` en el YAML y `web_collector.py` asignado a Sprint 1b | v1 describía scraping sin entrada de ejemplo y sin sprint. |
| 7 | IoCs crudos en almacenamiento; defang solo en presentación | v1 prometía "listos para copiar a SIEM/EDR" con valores defanged. |
| 8 | `iocs` con `estado_verificacion` y `confianza`; exclusión de rangos RFC 5737 y hashes triviales | v1 llamaba "verificados" a IoCs que solo pasaban una lista blanca; su ejemplo usaba rangos de documentación y el hash de un archivo vacío. |
| 9 | `cves` como lista y `tipo` de incidente | v1 permitía un solo `cve_id` por incidente. |
| 10 | Contrato del ítem crudo, deduplicación de ingesta, criterios de aceptación | v1 no definía la interfaz de salida de la ingesta. |
| 11 | Circuit breaker con estados y umbrales propuestos | v1 lo nombraba sin definirlo. |
| 12 | Sección de seguridad (SSRF, XXE, secretos, auth, RBAC) | v1 no la contemplaba. |
| 13 | Scheduler en proceso único | Riesgo de ejecución duplicada con múltiples workers. |
| 14 | Se declaran limitaciones del scraping y de la validación de PoC | v1 prometía resolver "de raíz" y trataba cualquier repositorio como PoC funcional. |
| 15 | Se agrega módulo `summarizer.py` y la decisión de mecanismo | v1 exigía resúmenes y mitigaciones sin módulo ni método. |
| 16 | Se elimina "lógica estilo Jev AI" y se reemplaza por reglas explícitas | Término no definido en v1. |

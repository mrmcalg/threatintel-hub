# tests/test_web_collector.py
# Las páginas de este archivo son SINTÉTICAS. Excepción: la prueba con
# Trafilatura real usa HTML escrito a mano, no una página de Internet.
import re
from datetime import datetime, timezone

import pytest

from core.ingestion.base import Validadores
from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import ErrorHttp, Respuesta
from core.ingestion.web_collector import (
    AccesoProhibidoPorRobots,
    Extraccion,
    RenderJsNoDisponible,
    WebCollector,
    decodificar_html,
    extraer_enlaces,
)
from core.models.source_config import Defaults, Fuente

pytestmark = pytest.mark.anyio

LISTADO_URL = "https://ejemplo.com/advisories/"
ROBOTS_URL = "https://ejemplo.com/robots.txt"
AVISO_1 = "https://ejemplo.com/advisories/aviso-1"
AVISO_2 = "https://ejemplo.com/advisories/aviso-2"
PATRON = "/advisories/[a-z0-9-]+"
AHORA = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)

DEFAULTS = Defaults(
    timeout_segundos=20,
    max_bytes=5242880,
    reintentos=3,
    user_agent="ThreatIntelHub/2.0 (+contacto@ejemplo.org)",
    respetar_robots_txt=True,
)
DEFAULTS_SIN_ROBOTS = Defaults(
    timeout_segundos=20,
    max_bytes=5242880,
    reintentos=3,
    user_agent="ThreatIntelHub/2.0 (+contacto@ejemplo.org)",
    respetar_robots_txt=False,
)

LISTADO_HTML = """<html><body>
<a href="/advisories/aviso-1">Aviso 1</a>
<a href="/advisories/aviso-2?utm_source=x">Aviso 2</a>
<a href="https://otro-sitio.com/advisories/aviso-3">Externo</a>
<a href="/contacto">Contacto</a>
<a href="/advisories/aviso-1#comentarios">Aviso 1 otra vez</a>
</body></html>"""

ROBOTS_ABIERTO = b"User-agent: *\nDisallow:\n"


def resp(cuerpo: bytes, url: str, etag=None, status=200) -> Respuesta:
    return Respuesta(status=status, contenido=cuerpo, url_final=url, etag=etag)


def articulo(n: int, texto=None, charset="utf-8") -> bytes:
    cuerpo = texto if texto is not None else f"contenido {n} " * 60
    html = (
        f'<html><head><meta charset="{charset}"><title>Aviso {n}</title></head>'
        f"<body><p>{cuerpo}</p></body></html>"
    )
    return html.encode(charset)


def extractor_falso(html: str) -> Extraccion:
    m = re.search(r"<p>(.*?)</p>", html, re.S)
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    return Extraccion(texto=m.group(1) if m else "", titulo=t.group(1) if t else "")


class ClienteGuion:
    """Por URL devuelve (o lanza) las acciones en orden; la última se repite."""

    def __init__(self, guion: dict):
        self._guion = {url: list(acciones) for url, acciones in guion.items()}
        self.llamadas: list[tuple] = []

    async def obtener(self, url, *, etag=None, last_modified=None):
        self.llamadas.append((url, etag, last_modified))
        acciones = self._guion[url]
        accion = acciones.pop(0) if len(acciones) > 1 else acciones[0]
        if isinstance(accion, Exception):
            raise accion
        return accion

    def urls(self) -> list[str]:
        return [u for u, _, _ in self.llamadas]


def guion_base(**cambios) -> dict:
    guion = {
        ROBOTS_URL: [ErrorHttp(404, ROBOTS_URL)],
        LISTADO_URL: [resp(LISTADO_HTML.encode("utf-8"), LISTADO_URL, etag='"L1"')],
        AVISO_1: [resp(articulo(1), AVISO_1)],
        AVISO_2: [resp(articulo(2), AVISO_2)],
    }
    guion.update(cambios)
    return guion


def hacer_fuente(tipo="web", render_js=False) -> Fuente:
    datos = {
        "id": "sitio_web",
        "nombre": "Sitio de avisos",
        "tipo": tipo,
        "url": LISTADO_URL,
        "activo": True,
        "categoria": "avisos_fabricante",
        "frecuencia_minutos": 120,
        "fiabilidad": "alta",
    }
    if tipo == "web":
        datos["web"] = {"modo": "listado", "patron_enlaces": PATRON, "render_js": render_js}
    return Fuente(**datos)


async def sin_pausa(segundos):
    return None


def hacer_colector(guion, *, defaults=DEFAULTS, ya_visto=None, max_articulos=30, render_js=False):
    cliente = ClienteGuion(guion)
    colector = WebCollector(
        hacer_fuente(render_js=render_js),
        cliente,
        ahora=lambda: AHORA,
        defaults=defaults,
        ya_visto=ya_visto,
        extractor=extractor_falso,
        dormir=sin_pausa,
        max_articulos=max_articulos,
        pausa_segundos=0,
    )
    return colector, cliente


# ------------------------------------------------------------ enlaces y texto
def test_extrae_enlaces_del_mismo_sitio_sin_repetidos():
    enlaces = extraer_enlaces(LISTADO_HTML, LISTADO_URL, PATRON)
    assert [c for _, c in enlaces] == [AVISO_1, AVISO_2]
    assert enlaces[1][0] == "https://ejemplo.com/advisories/aviso-2?utm_source=x"


def test_decodifica_segun_el_meta_charset():
    pagina = '<meta charset="iso-8859-1"><p>canción</p>'.encode("iso-8859-1")
    assert "canción" in decodificar_html(pagina)


# ------------------------------------------------------------------ flujo normal
async def test_descarga_los_articulos_nuevos_y_arma_el_contrato():
    colector, _ = hacer_colector(guion_base())
    r = await colector.recolectar()
    assert r.status == 200
    assert len(r.items) == 2
    assert r.omitidos == 0
    assert r.advertencias == []
    assert r.etag == '"L1"'

    i = r.items[0]
    assert i.tipo_fuente == "web"
    assert i.fuente_id == "sitio_web"
    assert i.guid is None
    assert i.url_canonica == AVISO_1
    assert i.titulo == "Aviso 1"
    assert i.metodo_extraccion == "trafilatura"
    assert i.calidad_extraccion == "completa"
    assert i.estado == "nuevo"
    assert i.fecha_captura == AHORA
    assert i.fecha_publicacion is None
    assert r.items[1].url_original == "https://ejemplo.com/advisories/aviso-2?utm_source=x"
    assert r.items[1].url_canonica == AVISO_2


async def test_texto_corto_se_guarda_como_parcial():
    guion = guion_base(**{AVISO_1: [resp(articulo(1, texto="breve"), AVISO_1)]})
    colector, _ = hacer_colector(guion)
    r = await colector.recolectar()
    assert r.items[0].calidad_extraccion == "parcial"
    assert r.items[1].calidad_extraccion == "completa"


async def test_articulo_sin_texto_extraible_se_omite():
    guion = guion_base(**{AVISO_1: [resp(articulo(1, texto=""), AVISO_1)]})
    colector, _ = hacer_colector(guion)
    r = await colector.recolectar()
    assert [i.url_canonica for i in r.items] == [AVISO_2]
    assert r.omitidos == 1
    assert any("sin texto" in a for a in r.advertencias)


async def test_listado_sin_enlaces_que_coincidan_genera_advertencia():
    guion = guion_base(**{LISTADO_URL: [resp(b"<html><body>nada</body></html>", LISTADO_URL)]})
    colector, _ = hacer_colector(guion)
    r = await colector.recolectar()
    assert r.items == []
    assert any("patron_enlaces" in a for a in r.advertencias)


async def test_codificacion_latin1_se_decodifica_bien():
    texto = "acción " * 100
    guion = guion_base(**{AVISO_1: [resp(articulo(1, texto=texto, charset="iso-8859-1"), AVISO_1)]})
    colector, _ = hacer_colector(guion)
    r = await colector.recolectar()
    assert "acción" in r.items[0].texto


# --------------------------------------------------------- ya visto y límites
async def test_articulo_ya_visto_no_se_descarga():
    colector, cliente = hacer_colector(
        guion_base(), ya_visto=lambda fuente_id, canonica: canonica == AVISO_1
    )
    r = await colector.recolectar()
    assert [i.url_canonica for i in r.items] == [AVISO_2]
    assert AVISO_1 not in cliente.urls()


async def test_tope_de_articulos_deja_el_resto_y_no_guarda_validadores():
    colector, cliente = hacer_colector(guion_base(), max_articulos=1)
    r = await colector.recolectar()
    assert len(r.items) == 1
    assert any("se procesan 1" in a for a in r.advertencias)
    assert r.etag is None
    assert AVISO_2 not in cliente.urls()


async def test_error_en_un_articulo_no_detiene_a_los_demas():
    guion = guion_base(**{AVISO_1: [ErrorHttp(404, AVISO_1)]})
    colector, _ = hacer_colector(guion)
    r = await colector.recolectar()
    assert [i.url_canonica for i in r.items] == [AVISO_2]
    assert r.omitidos == 1
    assert any("aviso-1" in a for a in r.advertencias)
    assert r.etag is None


async def test_304_del_listado_conserva_los_validadores():
    guion = guion_base(**{LISTADO_URL: [Respuesta(status=304, contenido=b"", url_final=LISTADO_URL)]})
    colector, cliente = hacer_colector(guion)
    r = await colector.recolectar(Validadores(etag='"L0"', last_modified="X"))
    assert r.no_modificado is True
    assert r.items == []
    assert r.etag == '"L0"'
    assert (LISTADO_URL, '"L0"', "X") in cliente.llamadas


# ---------------------------------------------------------------- robots.txt
async def test_robots_bloquea_un_articulo_concreto():
    robots = b"User-agent: *\nDisallow: /advisories/aviso-2\n"
    colector, cliente = hacer_colector(guion_base(**{ROBOTS_URL: [resp(robots, ROBOTS_URL)]}))
    r = await colector.recolectar()
    assert [i.url_canonica for i in r.items] == [AVISO_1]
    assert r.omitidos == 1
    assert any("robots.txt" in a for a in r.advertencias)
    assert AVISO_2 not in cliente.urls()


async def test_robots_que_prohibe_el_listado_hace_fallar_la_fuente():
    robots = b"User-agent: *\nDisallow: /advisories/\n"
    colector, cliente = hacer_colector(guion_base(**{ROBOTS_URL: [resp(robots, ROBOTS_URL)]}))
    with pytest.raises(AccesoProhibidoPorRobots):
        await colector.recolectar()
    assert LISTADO_URL not in cliente.urls()


async def test_robots_no_disponible_por_error_de_servidor_se_trata_como_prohibido():
    colector, _ = hacer_colector(guion_base(**{ROBOTS_URL: [ErrorHttp(503, ROBOTS_URL)]}))
    with pytest.raises(AccesoProhibidoPorRobots, match="no disponible"):
        await colector.recolectar()


async def test_robots_se_consulta_una_sola_vez_por_host():
    colector, cliente = hacer_colector(guion_base(**{ROBOTS_URL: [resp(ROBOTS_ABIERTO, ROBOTS_URL)]}))
    await colector.recolectar()
    assert cliente.urls().count(ROBOTS_URL) == 1


async def test_no_consulta_robots_si_la_configuracion_lo_desactiva():
    colector, cliente = hacer_colector(guion_base(), defaults=DEFAULTS_SIN_ROBOTS)
    r = await colector.recolectar()
    assert len(r.items) == 2
    assert ROBOTS_URL not in cliente.urls()


# --------------------------------------------------------------- validaciones
async def test_render_js_no_esta_disponible():
    colector, _ = hacer_colector(guion_base(), render_js=True)
    with pytest.raises(RenderJsNoDisponible):
        await colector.recolectar()


def test_el_colector_rechaza_fuentes_de_otro_tipo():
    with pytest.raises(ValueError, match="solo acepta"):
        WebCollector(hacer_fuente(tipo="rss"), ClienteGuion({}), defaults=DEFAULTS)


def test_el_colector_exige_los_defaults():
    with pytest.raises(ValueError, match="defaults"):
        WebCollector(hacer_fuente(), ClienteGuion({}), defaults=None)


# --------------------------------------------------------------- integraciones
async def test_segunda_pasada_no_vuelve_a_descargar_articulos_ya_guardados():
    store = ItemStore(":memory:")
    try:
        colector, cliente = hacer_colector(guion_base(), ya_visto=store.existe_url)
        primera = await colector.recolectar()
        store.registrar_lote(primera.items)
        segunda = await colector.recolectar()

        assert len(primera.items) == 2
        assert segunda.items == []
        assert cliente.urls().count(AVISO_1) == 1
        assert store.contar_items() == 2
    finally:
        store.cerrar()


async def test_trafilatura_real_extrae_el_texto_principal():
    pytest.importorskip("trafilatura")
    parrafo = (
        "Este aviso describe una vulnerabilidad en el producto y recomienda "
        "aplicar la actualización del fabricante. "
    ) * 8
    html = (
        "<html><head><title>Aviso real</title></head><body>"
        "<nav><a href='/'>Inicio</a></nav>"
        f"<article><h1>Aviso real</h1><p>{parrafo}</p><p>{parrafo}</p></article>"
        "<script>var secreto = 'no-debe-aparecer';</script></body></html>"
    ).encode("utf-8")
    guion = guion_base(**{AVISO_1: [resp(html, AVISO_1)], AVISO_2: [resp(html, AVISO_2)]})
    colector = WebCollector(
        hacer_fuente(),
        ClienteGuion(guion),
        ahora=lambda: AHORA,
        defaults=DEFAULTS_SIN_ROBOTS,
        dormir=sin_pausa,
        pausa_segundos=0,
    )
    r = await colector.recolectar()
    assert len(r.items) >= 1
    assert "vulnerabilidad" in r.items[0].texto
    assert "no-debe-aparecer" not in r.items[0].texto

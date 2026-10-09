# tests/test_runner.py
import asyncio
import json

import pytest

from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import ErrorHttp, Respuesta
from core.ingestion.runner import IngestionRunner
from core.models.source_config import SourcesFile

pytestmark = pytest.mark.anyio

DEFAULTS = {
    "timeout_segundos": 20,
    "max_bytes": 5242880,
    "reintentos": 3,
    "user_agent": "Test/1.0",
    "respetar_robots_txt": True,
}


def url_de(fuente_id: str) -> str:
    return f"https://ejemplo.com/{fuente_id}"


def definir_fuente(fuente_id, tipo="rss", activo=True):
    datos = {
        "id": fuente_id,
        "nombre": fuente_id,
        "tipo": tipo,
        "url": url_de(fuente_id),
        "activo": activo,
        "categoria": "noticias_soc",
        "frecuencia_minutos": 30,
        "fiabilidad": "media",
    }
    if tipo == "web":
        datos["web"] = {"modo": "listado", "patron_enlaces": "/a/[a-z]+", "render_js": False}
    return datos


def hacer_config(*fuentes) -> SourcesFile:
    return SourcesFile.model_validate(
        {"version": 2, "defaults": DEFAULTS, "fuentes": list(fuentes)}
    )


def rss_con(n: int, prefijo: str) -> bytes:
    items = "".join(
        f"<item><title>{prefijo} {i}</title>"
        f"<link>https://ejemplo.com/{prefijo}/{i}</link>"
        f"<description>texto {prefijo} {i}</description></item>"
        for i in range(n)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<link>https://ejemplo.com/</link><description>d</description>{items}"
        "</channel></rss>"
    ).encode("utf-8")


def kev_json() -> bytes:
    vuln = {
        "cveID": "CVE-2026-10001",
        "vendorProject": "P",
        "product": "X",
        "vulnerabilityName": "N",
        "dateAdded": "2026-10-07",
        "shortDescription": "d",
        "requiredAction": "a",
        "dueDate": "2026-10-28",
        "knownRansomwareCampaignUse": "Unknown",
        "notes": "",
    }
    return json.dumps({"count": 1, "vulnerabilities": [vuln]}).encode("utf-8")


def ok(cuerpo: bytes, fuente_id: str, etag=None) -> Respuesta:
    return Respuesta(status=200, contenido=cuerpo, url_final=url_de(fuente_id), etag=etag)


class ClienteGuion:
    """Cliente falso: por URL devuelve (o lanza) las acciones en orden; la
    última se repite."""

    def __init__(self, guion: dict, demora: float = 0.0):
        self._guion = {url: list(acciones) for url, acciones in guion.items()}
        self._demora = demora
        self.llamadas: list[tuple] = []
        self._activas = 0
        self.max_activas = 0

    async def obtener(self, url, *, etag=None, last_modified=None):
        self.llamadas.append((url, etag, last_modified))
        self._activas += 1
        self.max_activas = max(self.max_activas, self._activas)
        try:
            if self._demora:
                await asyncio.sleep(self._demora)
            acciones = self._guion[url]
            accion = acciones.pop(0) if len(acciones) > 1 else acciones[0]
        finally:
            self._activas -= 1
        if isinstance(accion, Exception):
            raise accion
        return accion


@pytest.fixture
def store():
    s = ItemStore(":memory:")
    yield s
    s.cerrar()


def por_id(resumenes):
    return {r.fuente_id: r for r in resumenes}


# ------------------------------------------------------------- flujo normal
async def test_ejecuta_fuentes_rss_y_api_y_guarda_los_items(store):
    config = hacer_config(definir_fuente("feed_a"), definir_fuente("kev", tipo="api"))
    cliente = ClienteGuion(
        {
            url_de("feed_a"): [ok(rss_con(3, "a"), "feed_a")],
            url_de("kev"): [ok(kev_json(), "kev")],
        }
    )
    r = por_id(await IngestionRunner(config, store, cliente).ejecutar())
    assert r["feed_a"].resultado == "ok" and r["feed_a"].nuevos == 3
    assert r["kev"].resultado == "ok" and r["kev"].nuevos == 1
    assert store.contar_items() == 4


async def test_segunda_ejecucion_no_crea_duplicados(store):
    config = hacer_config(definir_fuente("feed_a"))
    cliente = ClienteGuion({url_de("feed_a"): [ok(rss_con(3, "a"), "feed_a")]})
    runner = IngestionRunner(config, store, cliente)
    primera = (await runner.ejecutar())[0]
    segunda = (await runner.ejecutar())[0]
    assert primera.nuevos == 3
    assert segunda.nuevos == 0 and segunda.duplicados == 3
    assert store.contar_items() == 3


async def test_guarda_validadores_y_los_envia_en_la_siguiente_ejecucion(store):
    url = url_de("feed_a")
    config = hacer_config(definir_fuente("feed_a"))
    cliente = ClienteGuion(
        {
            url: [
                ok(rss_con(2, "a"), "feed_a", etag='"v1"'),
                Respuesta(status=304, contenido=b"", url_final=url),
            ]
        }
    )
    runner = IngestionRunner(config, store, cliente)
    primera = (await runner.ejecutar())[0]
    segunda = (await runner.ejecutar())[0]
    assert primera.resultado == "ok"
    assert segunda.resultado == "sin_cambios"
    assert cliente.llamadas[0] == (url, None, None)
    assert cliente.llamadas[1] == (url, '"v1"', None)
    assert store.obtener_estado_fuente("feed_a").etag == '"v1"'
    assert store.contar_items() == 2


async def test_registra_cada_ejecucion(store):
    config = hacer_config(definir_fuente("feed_a"))
    cliente = ClienteGuion({url_de("feed_a"): [ok(rss_con(3, "a"), "feed_a")]})
    await IngestionRunner(config, store, cliente).ejecutar()
    filas = store.listar_ejecuciones("feed_a")
    assert len(filas) == 1
    assert filas[0]["http_status"] == 200
    assert filas[0]["nuevos"] == 3
    assert filas[0]["error"] is None


# --------------------------------------------------------- aislamiento de fallos
async def test_una_fuente_caida_no_detiene_a_las_demas(store):
    config = hacer_config(definir_fuente("feed_a"), definir_fuente("feed_b"))
    cliente = ClienteGuion(
        {
            url_de("feed_a"): [ErrorHttp(503, url_de("feed_a"))],
            url_de("feed_b"): [ok(rss_con(2, "b"), "feed_b")],
        }
    )
    r = por_id(await IngestionRunner(config, store, cliente).ejecutar())
    assert r["feed_a"].resultado == "error"
    assert "503" in r["feed_a"].detalle
    assert r["feed_b"].resultado == "ok" and r["feed_b"].nuevos == 2
    assert store.contar_items("feed_b") == 2
    assert store.contar_items("feed_a") == 0

    fila = store.listar_ejecuciones("feed_a")[0]
    assert fila["http_status"] == 503
    assert "503" in fila["error"]


async def test_una_excepcion_inesperada_tampoco_detiene_a_las_demas(store):
    config = hacer_config(definir_fuente("feed_a"), definir_fuente("feed_b"))
    cliente = ClienteGuion(
        {
            url_de("feed_a"): [RuntimeError("fallo imprevisto")],
            url_de("feed_b"): [ok(rss_con(1, "b"), "feed_b")],
        }
    )
    r = por_id(await IngestionRunner(config, store, cliente).ejecutar())
    assert r["feed_a"].resultado == "error"
    assert "RuntimeError" in r["feed_a"].detalle
    assert r["feed_b"].resultado == "ok"


async def test_los_fallos_consecutivos_se_acumulan_y_se_reinician(store):
    url = url_de("feed_a")
    config = hacer_config(definir_fuente("feed_a"))
    cliente = ClienteGuion(
        {url: [ErrorHttp(503, url), ErrorHttp(503, url), ok(rss_con(1, "a"), "feed_a")]}
    )
    runner = IngestionRunner(config, store, cliente)

    await runner.ejecutar()
    await runner.ejecutar()
    estado = store.obtener_estado_fuente("feed_a")
    assert estado.fallos_consecutivos == 2
    assert "503" in estado.ultimo_error

    await runner.ejecutar()
    estado = store.obtener_estado_fuente("feed_a")
    assert estado.fallos_consecutivos == 0
    assert estado.ultimo_error is None
    assert estado.ultimo_exito is not None


# ------------------------------------------------------ fuentes que no se ejecutan
async def test_fuente_inactiva_no_se_ejecuta(store):
    config = hacer_config(definir_fuente("feed_a", activo=False))
    cliente = ClienteGuion({})
    r = (await IngestionRunner(config, store, cliente).ejecutar())[0]
    assert r.resultado == "pausada"
    assert cliente.llamadas == []


async def test_fuente_web_consulta_robots_y_luego_el_listado(store):
    url_robots = "https://ejemplo.com/robots.txt"
    listado = b"<html><body><p>sin enlaces</p></body></html>"
    config = hacer_config(definir_fuente("sitio_web", tipo="web"))
    cliente = ClienteGuion(
        {
            url_robots: [ErrorHttp(404, url_robots)],
            url_de("sitio_web"): [ok(listado, "sitio_web")],
        }
    )
    r = (await IngestionRunner(config, store, cliente).ejecutar())[0]
    assert r.resultado == "ok"
    assert r.nuevos == 0
    assert any("patron_enlaces" in a for a in r.advertencias)
    assert [u for u, _, _ in cliente.llamadas] == [url_robots, url_de("sitio_web")]


async def test_solo_filtra_las_fuentes_y_rechaza_ids_desconocidos(store):
    config = hacer_config(definir_fuente("feed_a"), definir_fuente("feed_b"))
    cliente = ClienteGuion(
        {
            url_de("feed_a"): [ok(rss_con(1, "a"), "feed_a")],
            url_de("feed_b"): [ok(rss_con(1, "b"), "feed_b")],
        }
    )
    runner = IngestionRunner(config, store, cliente)
    resumenes = await runner.ejecutar(solo={"feed_a"})
    assert [r.fuente_id for r in resumenes] == ["feed_a"]
    assert all(url == url_de("feed_a") for url, _, _ in cliente.llamadas)
    with pytest.raises(ValueError, match="desconocidas"):
        await runner.ejecutar(solo={"no_existe"})


# ----------------------------------------------------------------- concurrencia
async def test_respeta_el_limite_de_concurrencia(store):
    ids = [f"feed_{i}" for i in range(5)]
    config = hacer_config(*[definir_fuente(i) for i in ids])
    cliente = ClienteGuion({url_de(i): [ok(rss_con(1, i), i)] for i in ids}, demora=0.01)
    await IngestionRunner(config, store, cliente, max_concurrentes=2).ejecutar()
    assert cliente.max_activas == 2

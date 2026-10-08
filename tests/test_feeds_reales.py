# tests/test_feeds_reales.py
# Prueba el colector RSS contra feeds REALES guardados en tests/fixtures/*.xml.
# Si no hay ningún .xml en esa carpeta, las pruebas se omiten (skip).
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import Respuesta
from core.ingestion.rss_collector import RssCollector
from core.models.source_config import Fuente

pytestmark = pytest.mark.anyio

CARPETA = Path(__file__).resolve().parent / "fixtures"
FIXTURES = sorted(CARPETA.glob("*.xml"))
PARAMETROS = FIXTURES or [
    pytest.param(
        None, marks=pytest.mark.skip(reason="No hay feeds reales en tests/fixtures/*.xml")
    )
]
AHORA = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)


class FalsoCliente:
    def __init__(self, contenido: bytes):
        self._contenido = contenido

    async def obtener(self, url, *, etag=None, last_modified=None):
        return Respuesta(status=200, contenido=self._contenido, url_final=url)


def hacer_colector(ruta: Path) -> RssCollector:
    fuente_id = re.sub(r"[^a-z0-9]+", "_", ruta.stem.lower()).strip("_") or "feed"
    fuente = Fuente(
        id=fuente_id,
        nombre=ruta.stem,
        tipo="rss",
        url="https://ejemplo.com/feed",
        activo=True,
        categoria="noticias_soc",
        frecuencia_minutos=30,
        fiabilidad="media",
    )
    return RssCollector(fuente, FalsoCliente(ruta.read_bytes()), ahora=lambda: AHORA)


@pytest.mark.parametrize("ruta", PARAMETROS, ids=lambda r: r.name if r else "sin_fixtures")
async def test_feed_real_se_procesa(ruta):
    r = await hacer_colector(ruta).recolectar()

    sin_fecha = sum(1 for i in r.items if i.fecha_publicacion is None)
    calidad = Counter(i.calidad_extraccion for i in r.items)
    print(
        f"\n[{ruta.name}] items={len(r.items)} omitidos={r.omitidos} "
        f"sin_fecha={sin_fecha} calidad={dict(calidad)} advertencias={r.advertencias}"
    )

    assert len(r.items) > 0
    assert all(i.url_canonica.startswith(("http://", "https://")) for i in r.items)
    ids = [i.item_id for i in r.items]
    assert len(ids) == len(set(ids)), "item_id repetido dentro del mismo feed"


@pytest.mark.parametrize("ruta", PARAMETROS, ids=lambda r: r.name if r else "sin_fixtures")
async def test_feed_real_es_idempotente(ruta):
    colector = hacer_colector(ruta)
    store = ItemStore(":memory:")
    try:
        for item in (await colector.recolectar()).items:
            store.registrar(item)
        cuenta = store.contar_items()

        segunda = [store.registrar(i).estado for i in (await colector.recolectar()).items]

        assert set(segunda) == {"duplicado"}
        assert store.contar_items() == cuenta
    finally:
        store.cerrar()

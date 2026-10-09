# tests/test_consultas_store.py
from datetime import datetime, timezone

from core.ingestion.dedup import ItemStore
from core.ingestion.normalizer import canonical_url, content_hash, make_item_id
from core.models.raw_item import RawItem


def hacer_item(url, texto, fuente_id="feed_a"):
    canon = canonical_url(url)
    return RawItem(
        item_id=make_item_id(fuente_id, canon),
        fuente_id=fuente_id,
        tipo_fuente="rss",
        url_original=url,
        url_canonica=canon,
        titulo=f"Titulo {texto}",
        fecha_captura=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
        texto=texto,
        hash_contenido=content_hash(texto),
        metodo_extraccion="feed_summary",
        calidad_extraccion="solo_resumen",
        estado="nuevo",
    )


def test_existe_url_distingue_fuente_y_url():
    store = ItemStore(":memory:")
    try:
        store.registrar(hacer_item("https://ejemplo.com/a", "uno"))
        assert store.existe_url("feed_a", "https://ejemplo.com/a") is True
        assert store.existe_url("feed_a", "https://ejemplo.com/b") is False
        assert store.existe_url("feed_b", "https://ejemplo.com/a") is False
    finally:
        store.cerrar()


def test_listar_items_devuelve_los_ultimos_insertados_primero():
    store = ItemStore(":memory:")
    try:
        for i in range(3):
            store.registrar(hacer_item(f"https://ejemplo.com/{i}", f"texto {i}"))
        items = store.listar_items("feed_a", limite=2)
        assert [i.url_canonica for i in items] == [
            "https://ejemplo.com/2",
            "https://ejemplo.com/1",
        ]
    finally:
        store.cerrar()


def test_listar_items_filtra_por_fuente():
    store = ItemStore(":memory:")
    try:
        store.registrar(hacer_item("https://ejemplo.com/a", "uno", fuente_id="feed_a"))
        store.registrar(hacer_item("https://ejemplo.com/b", "dos", fuente_id="feed_b"))
        assert [i.fuente_id for i in store.listar_items("feed_b")] == ["feed_b"]
        assert store.listar_items("no_existe") == []
    finally:
        store.cerrar()

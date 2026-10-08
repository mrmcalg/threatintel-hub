# tests/test_estado_fuentes.py
from datetime import datetime, timezone

from core.ingestion.dedup import ItemStore
from core.ingestion.normalizer import canonical_url, content_hash, make_item_id
from core.models.raw_item import RawItem


def hacer_item(url="https://ejemplo.com/a", texto="texto uno", fuente_id="feed_a"):
    canon = canonical_url(url)
    return RawItem(
        item_id=make_item_id(fuente_id, canon),
        fuente_id=fuente_id,
        tipo_fuente="rss",
        url_original=url,
        url_canonica=canon,
        titulo="Titulo",
        fecha_captura=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
        texto=texto,
        hash_contenido=content_hash(texto),
        metodo_extraccion="feed_summary",
        calidad_extraccion="solo_resumen",
        estado="nuevo",
    )


def test_registrar_lote_clasifica_en_una_transaccion():
    store = ItemStore(":memory:")
    try:
        lote = [
            hacer_item(url="https://ejemplo.com/a", texto="uno"),
            hacer_item(url="https://ejemplo.com/b", texto="dos"),
            hacer_item(url="https://ejemplo.com/a", texto="uno"),  # repetido dentro del lote
        ]
        estados = [i.estado for i in store.registrar_lote(lote)]
        assert estados == ["nuevo", "nuevo", "duplicado"]
        assert store.contar_items() == 2
    finally:
        store.cerrar()


def test_estado_inicial_de_una_fuente_desconocida():
    store = ItemStore(":memory:")
    try:
        e = store.obtener_estado_fuente("nunca_vista")
        assert e.etag is None and e.last_modified is None
        assert e.ultimo_exito is None
        assert e.fallos_consecutivos == 0
    finally:
        store.cerrar()


def test_exito_guarda_validadores_y_un_304_conserva_los_previos():
    store = ItemStore(":memory:")
    try:
        store.registrar_exito_fuente("feed_a", '"v1"', "Wed, 07 Oct 2026 10:00:00 GMT")
        store.registrar_exito_fuente("feed_a", None, None)  # p. ej. un 304 sin cabeceras
        e = store.obtener_estado_fuente("feed_a")
        assert e.etag == '"v1"'
        assert e.last_modified == "Wed, 07 Oct 2026 10:00:00 GMT"
        assert e.ultimo_exito is not None
    finally:
        store.cerrar()


def test_fallo_incrementa_conserva_validadores_y_el_exito_reinicia():
    store = ItemStore(":memory:")
    try:
        store.registrar_exito_fuente("feed_a", '"v1"', None)
        store.registrar_fallo_fuente("feed_a", "HTTP 503")
        store.registrar_fallo_fuente("feed_a", "HTTP 503")
        e = store.obtener_estado_fuente("feed_a")
        assert e.fallos_consecutivos == 2
        assert e.ultimo_error == "HTTP 503"
        assert e.etag == '"v1"'

        store.registrar_exito_fuente("feed_a", None, None)
        e = store.obtener_estado_fuente("feed_a")
        assert e.fallos_consecutivos == 0
        assert e.ultimo_error is None
    finally:
        store.cerrar()


def test_el_estado_sobrevive_al_reinicio(tmp_path):
    db = tmp_path / "datos" / "estado.sqlite"
    s1 = ItemStore(db)
    s1.registrar_exito_fuente("feed_a", '"v9"', None)
    s1.cerrar()

    s2 = ItemStore(db)
    etag = s2.obtener_estado_fuente("feed_a").etag
    s2.cerrar()
    assert etag == '"v9"'

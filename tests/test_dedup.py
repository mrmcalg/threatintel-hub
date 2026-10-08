# tests/test_dedup.py
from datetime import datetime, timezone

import pytest

from core.ingestion.dedup import ItemStore
from core.ingestion.normalizer import canonical_url, content_hash, make_item_id
from core.models.raw_item import RawItem


def hacer_item(url="https://ejemplo.com/a", texto="texto uno", fuente_id="feed_a", guid=None):
    canon = canonical_url(url)
    return RawItem(
        item_id=make_item_id(fuente_id, canon),
        fuente_id=fuente_id,
        tipo_fuente="rss",
        url_original=url,
        url_canonica=canon,
        guid=guid,
        titulo="Titulo",
        fecha_captura=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
        texto=texto,
        hash_contenido=content_hash(texto),
        metodo_extraccion="feed_summary",
        calidad_extraccion="solo_resumen",
        estado="nuevo",
    )


@pytest.fixture
def store():
    s = ItemStore(":memory:")
    yield s
    s.cerrar()


def test_primer_ingreso_es_nuevo(store):
    r = store.registrar(hacer_item())
    assert r.estado == "nuevo"
    assert store.contar_items() == 1


def test_reingesta_identica_es_duplicado_e_idempotente(store):
    store.registrar(hacer_item())
    r = store.registrar(hacer_item())
    assert r.estado == "duplicado"
    assert store.contar_items() == 1


def test_mismo_guid_con_texto_cambiado_es_actualizado(store):
    primero = store.registrar(hacer_item(url="https://ejemplo.com/a", guid="g1"))
    r = store.registrar(hacer_item(url="https://ejemplo.com/b", guid="g1", texto="texto modificado"))
    assert r.estado == "actualizado"
    assert r.item_id == primero.item_id
    assert store.contar_items() == 1


def test_misma_url_sin_guid_con_texto_cambiado_es_actualizado(store):
    store.registrar(hacer_item())
    r = store.registrar(hacer_item(texto="texto modificado"))
    assert r.estado == "actualizado"
    assert store.contar_items() == 1


def test_mismo_texto_en_url_distinta_es_duplicado(store):
    store.registrar(hacer_item(url="https://ejemplo.com/a"))
    r = store.registrar(hacer_item(url="https://ejemplo.com/b"))
    assert r.estado == "duplicado"
    assert store.contar_items() == 1


def test_misma_url_en_otra_fuente_es_nuevo(store):
    store.registrar(hacer_item(fuente_id="feed_a"))
    r = store.registrar(hacer_item(fuente_id="feed_b"))
    assert r.estado == "nuevo"
    assert store.contar_items() == 2


def test_lote_repetido_no_crea_duplicados(store):
    lote = [hacer_item(url=f"https://ejemplo.com/{i}", texto=f"texto {i}") for i in range(3)]
    primera = [store.registrar(i).estado for i in lote]
    segunda = [store.registrar(i).estado for i in lote]
    assert primera == ["nuevo"] * 3
    assert segunda == ["duplicado"] * 3
    assert store.contar_items() == 3


def test_registra_ejecucion(store):
    inicio = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    fin = datetime(2026, 10, 8, 12, 0, 2, tzinfo=timezone.utc)
    store.registrar_ejecucion("feed_a", inicio, fin, 200, nuevos=2, actualizados=0, duplicados=1)
    filas = store.listar_ejecuciones("feed_a")
    assert len(filas) == 1
    assert filas[0]["http_status"] == 200
    assert filas[0]["nuevos"] == 2
    assert filas[0]["error"] is None


def test_la_deduplicacion_sobrevive_al_reinicio(tmp_path):
    db = tmp_path / "datos" / "prueba.sqlite"
    s1 = ItemStore(db)
    s1.registrar(hacer_item())
    s1.cerrar()

    s2 = ItemStore(db)
    r = s2.registrar(hacer_item())
    cuenta = s2.contar_items()
    s2.cerrar()

    assert r.estado == "duplicado"
    assert cuenta == 1

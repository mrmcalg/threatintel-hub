# tests/test_rss_collector.py
# Los feeds de este archivo son SINTÉTICOS (escritos a mano). Conviene añadir
# después 2 o 3 feeds reales en tests/fixtures/ (ver README / instrucciones).
from datetime import datetime, timezone

import pytest

from core.ingestion.base import (
    ContenidoNoSeguro,
    FeedInvalido,
    Validadores,
)
from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import Respuesta
from core.ingestion.rss_collector import RssCollector
from core.models.source_config import Fuente

pytestmark = pytest.mark.anyio

URL = "https://ejemplo.com/feed"
AHORA = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)

RSS_VALIDO = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Feed de prueba</title>
    <link>https://ejemplo.com/</link>
    <description>Prueba</description>
    <language>en-us</language>
    <item>
      <title>Aviso uno</title>
      <link>https://ejemplo.com/aviso-1?utm_source=rss</link>
      <guid isPermaLink="false">id-1</guid>
      <pubDate>Tue, 06 Oct 2026 09:30:00 -0500</pubDate>
      <description>&lt;p&gt;Resumen &lt;b&gt;uno&lt;/b&gt;&lt;/p&gt;</description>
    </item>
    <item>
      <title>Aviso dos</title>
      <link>https://ejemplo.com/aviso-2</link>
      <guid>id-2</guid>
      <description>Resumen dos</description>
      <content:encoded><![CDATA[<p>Texto <i>completo</i> dos</p><script>alert(1)</script>]]></content:encoded>
    </item>
    <item>
      <title>Aviso tres</title>
      <link>https://ejemplo.com/aviso-3</link>
      <description>Resumen tres</description>
    </item>
  </channel>
</rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom de prueba</title>
  <id>tag:ejemplo.com,2026:feed</id>
  <updated>2026-10-07T08:00:00Z</updated>
  <entry>
    <title>Entrada atom</title>
    <link rel="alternate" href="https://ejemplo.com/atom-1"/>
    <id>tag:ejemplo.com,2026:1</id>
    <updated>2026-10-07T08:00:00Z</updated>
    <author><name>Ana</name></author>
    <content type="html">&lt;p&gt;Contenido &lt;b&gt;atom&lt;/b&gt;&lt;/p&gt;</content>
  </entry>
</feed>"""

RSS_VACIO = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Vacio</title><link>https://ejemplo.com/</link>
<description>Sin entradas</description></channel></rss>"""

RSS_TRUNCADO = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>A</title><link>https://ejemplo.com/a</link><description>texto a</description></item>
<item><title>B</title><link>https://ejemplo.com/b</link>"""

PAGINA_HTML = "<html><body>Acceso denegado</body></html>"

RSS_SIN_ENLACE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title><link>https://ejemplo.com/</link>
<description>d</description>
<item><title>Sin enlace</title><description>x</description></item>
<item><title>Con enlace</title><link>https://ejemplo.com/ok</link><description>y</description></item>
</channel></rss>"""

RSS_SIN_TEXTO = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title><link>https://ejemplo.com/</link>
<description>d</description>
<item><title>Uno</title><link>https://ejemplo.com/a</link></item>
<item><title>Dos</title><link>https://ejemplo.com/b</link></item>
</channel></rss>"""

BOMBA_DE_ENTIDADES = """<?xml version="1.0"?>
<!DOCTYPE lolz [ <!ENTITY lol "lol"> <!ENTITY lol2 "&lol;&lol;&lol;&lol;"> ]>
<rss version="2.0"><channel><title>&lol2;</title></channel></rss>"""

XXE = """<?xml version="1.0"?>
<!DOCTYPE feed [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<rss version="2.0"><channel><title>&xxe;</title></channel></rss>"""


class FalsoCliente:
    def __init__(self, respuesta: Respuesta):
        self.respuesta = respuesta
        self.llamadas: list[tuple] = []

    async def obtener(self, url, *, etag=None, last_modified=None):
        self.llamadas.append((url, etag, last_modified))
        return self.respuesta


def hacer_fuente(tipo="rss", **extra):
    datos = dict(
        id="feed_a",
        nombre="Feed A",
        tipo=tipo,
        url=URL,
        activo=True,
        categoria="noticias_soc",
        frecuencia_minutos=30,
        fiabilidad="media",
    )
    datos.update(extra)
    return Fuente(**datos)


def hacer_colector(xml, *, status=200, etag=None, last_modified=None):
    cuerpo = xml.encode("utf-8") if isinstance(xml, str) else xml
    resp = Respuesta(
        status=status,
        contenido=cuerpo,
        url_final=URL,
        etag=etag,
        last_modified=last_modified,
    )
    cliente = FalsoCliente(resp)
    return RssCollector(hacer_fuente(), cliente, ahora=lambda: AHORA), cliente


# ------------------------------------------------------------------ mapeo
async def test_rss_valido_mapea_el_contrato_del_item():
    colector, _ = hacer_colector(RSS_VALIDO, etag='"v1"')
    r = await colector.recolectar()
    assert r.status == 200
    assert r.etag == '"v1"'
    assert len(r.items) == 3
    assert r.omitidos == 0

    i = r.items[0]
    assert i.fuente_id == "feed_a"
    assert i.tipo_fuente == "rss"
    assert i.titulo == "Aviso uno"
    assert i.guid == "id-1"
    assert i.url_original == "https://ejemplo.com/aviso-1?utm_source=rss"
    assert i.url_canonica == "https://ejemplo.com/aviso-1"
    assert i.fecha_publicacion == datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc)
    assert i.fecha_captura == AHORA
    assert i.idioma == "en"
    assert i.texto == "Resumen uno"
    assert i.metodo_extraccion == "feed_summary"
    assert i.calidad_extraccion == "solo_resumen"
    assert i.estado == "nuevo"


async def test_content_encoded_tiene_prioridad_y_se_limpia_el_html():
    colector, _ = hacer_colector(RSS_VALIDO)
    r = await colector.recolectar()
    i = r.items[1]
    assert i.texto == "Texto completo dos"
    assert "alert" not in i.texto
    assert i.metodo_extraccion == "feed_content"
    assert i.calidad_extraccion == "completa"


async def test_entrada_sin_fecha_deja_fecha_publicacion_en_none():
    colector, _ = hacer_colector(RSS_VALIDO)
    r = await colector.recolectar()
    assert r.items[2].fecha_publicacion is None


async def test_atom_mapea_autor_fecha_y_contenido():
    colector, _ = hacer_colector(ATOM)
    r = await colector.recolectar()
    i = r.items[0]
    assert i.guid == "tag:ejemplo.com,2026:1"
    assert i.autor == "Ana"
    assert i.fecha_publicacion == datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    assert i.texto == "Contenido atom"
    assert i.metodo_extraccion == "feed_content"


# ------------------------------------------------------- feeds problemáticos
async def test_feed_vacio_devuelve_cero_items_sin_error():
    colector, _ = hacer_colector(RSS_VACIO)
    r = await colector.recolectar()
    assert r.status == 200
    assert r.items == []


async def test_feed_truncado_se_procesa_con_advertencia():
    colector, _ = hacer_colector(RSS_TRUNCADO)
    r = await colector.recolectar()
    assert len(r.items) >= 1
    assert r.items[0].url_canonica == "https://ejemplo.com/a"
    assert any("mal formado" in a for a in r.advertencias)


async def test_pagina_html_no_es_un_feed():
    colector, _ = hacer_colector(PAGINA_HTML)
    with pytest.raises(FeedInvalido):
        await colector.recolectar()


@pytest.mark.parametrize("xml", [BOMBA_DE_ENTIDADES, XXE])
async def test_xml_con_entidades_se_rechaza(xml):
    colector, _ = hacer_colector(xml)
    with pytest.raises(ContenidoNoSeguro):
        await colector.recolectar()


async def test_entrada_sin_enlace_se_omite_y_se_cuenta():
    colector, _ = hacer_colector(RSS_SIN_ENLACE)
    r = await colector.recolectar()
    assert len(r.items) == 1
    assert r.items[0].titulo == "Con enlace"
    assert r.omitidos == 1
    assert any("omitidas" in a for a in r.advertencias)


async def test_entradas_sin_texto_no_comparten_hash():
    colector, _ = hacer_colector(RSS_SIN_TEXTO)
    r = await colector.recolectar()
    assert len(r.items) == 2
    assert all(i.texto == "" for i in r.items)
    assert r.items[0].hash_contenido != r.items[1].hash_contenido


# ----------------------------------------------------------- condicionales
async def test_304_conserva_los_validadores_previos():
    colector, cliente = hacer_colector(b"", status=304)
    r = await colector.recolectar(Validadores(etag='"abc"', last_modified="X"))
    assert r.no_modificado is True
    assert r.items == []
    assert r.etag == '"abc"'
    assert r.last_modified == "X"
    assert cliente.llamadas == [(URL, '"abc"', "X")]


# ---------------------------------------------------------- idempotencia
async def test_reingesta_del_mismo_feed_no_crea_duplicados():
    colector, _ = hacer_colector(RSS_VALIDO)
    store = ItemStore(":memory:")
    try:
        primera = [store.registrar(i).estado for i in (await colector.recolectar()).items]
        segunda = [store.registrar(i).estado for i in (await colector.recolectar()).items]
        assert primera == ["nuevo"] * 3
        assert segunda == ["duplicado"] * 3
        assert store.contar_items() == 3
    finally:
        store.cerrar()


# ------------------------------------------------------------- validaciones
def test_el_colector_rechaza_fuentes_de_otro_tipo():
    fuente_api = hacer_fuente(tipo="api")
    with pytest.raises(ValueError, match="solo acepta"):
        RssCollector(fuente_api, FalsoCliente(Respuesta(200, b"", URL)))

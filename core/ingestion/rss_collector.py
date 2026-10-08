# core/ingestion/rss_collector.py
"""Colector RSS/Atom (documento v2, secciones 4.4, 4.5 y 4.9).

Decisiones mías que el documento no precisa:
- Antes de feedparser se revisa el XML con defusedxml. Si contiene entidades
  o referencias externas, el feed se rechaza (ContenidoNoSeguro). Si solo está
  mal formado, se deja a feedparser, que es tolerante.
- Si una entrada no trae texto, el hash se calcula sobre título + URL canónica.
  Un hash del texto vacío sería igual para todas esas entradas y dedup.py las
  marcaría como duplicadas entre sí.
- Una entrada sin enlace http(s) se omite y se cuenta en `omitidos`.
"""
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

import feedparser
from defusedxml import ElementTree as defused_et
from defusedxml.common import DefusedXmlException

from core.ingestion.base import (
    BaseCollector,
    ContenidoNoSeguro,
    FeedInvalido,
    ResultadoRecoleccion,
    Validadores,
)
from core.ingestion.normalizer import (
    canonical_url,
    content_hash,
    make_item_id,
    normalize_text,
    to_utc,
)
from core.models.raw_item import RawItem


class _ExtractorTexto(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignorar += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignorar:
            self._ignorar -= 1

    def handle_data(self, data):
        if not self._ignorar:
            self.partes.append(data)


def html_a_texto(html: str) -> str:
    extractor = _ExtractorTexto()
    extractor.feed(html)
    extractor.close()
    return normalize_text(" ".join(extractor.partes))


def _verificar_xml_seguro(contenido: bytes) -> None:
    try:
        defused_et.fromstring(contenido)
    except DefusedXmlException as e:
        raise ContenidoNoSeguro(
            f"El feed contiene construcciones XML no permitidas: {e}"
        ) from e
    except (defused_et.ParseError, ValueError):
        return  # mal formado: lo trata feedparser, que es tolerante


def _fecha_entrada(entry) -> Optional[datetime]:
    for campo in ("published_parsed", "updated_parsed"):
        t = entry.get(campo)
        if t:
            try:
                # feedparser entrega estas tuplas ya convertidas a UTC.
                return to_utc(datetime(*t[:6]))
            except (ValueError, TypeError):
                continue
    return None


def _idioma(d) -> Optional[str]:
    lang = d.feed.get("language")
    if not lang:
        return None
    return lang.replace("_", "-").split("-")[0].lower() or None


class RssCollector(BaseCollector):
    tipo = "rss"

    async def recolectar(
        self, validadores: Optional[Validadores] = None
    ) -> ResultadoRecoleccion:
        previo = validadores or Validadores()
        resp = await self._descargar(validadores)

        if resp.no_modificado:
            return ResultadoRecoleccion(
                fuente_id=self._fuente.id,
                status=304,
                etag=resp.etag or previo.etag,
                last_modified=resp.last_modified or previo.last_modified,
            )

        _verificar_xml_seguro(resp.contenido)
        d = feedparser.parse(resp.contenido)
        if not d.get("version"):
            raise FeedInvalido(
                f"El contenido de '{self._fuente.id}' no es un feed RSS/Atom reconocible"
            )

        advertencias: list[str] = []
        if d.get("bozo"):
            advertencias.append("El feed está mal formado; se procesó lo posible")

        idioma = _idioma(d)
        ahora = self._ahora()
        items: list[RawItem] = []
        omitidos = 0
        for entry in d.entries:
            item = self._a_item(entry, idioma, ahora)
            if item is None:
                omitidos += 1
            else:
                items.append(item)
        if omitidos:
            advertencias.append(f"{omitidos} entradas omitidas por no tener enlace http(s)")

        return ResultadoRecoleccion(
            fuente_id=self._fuente.id,
            status=resp.status,
            items=items,
            etag=resp.etag,
            last_modified=resp.last_modified,
            omitidos=omitidos,
            advertencias=advertencias,
        )

    def _a_item(self, entry, idioma: Optional[str], ahora: datetime) -> Optional[RawItem]:
        enlace = (entry.get("link") or "").strip()
        guid = entry.get("id") or None
        if not enlace and guid and guid.lower().startswith(("http://", "https://")):
            enlace = guid
        if not enlace.lower().startswith(("http://", "https://")):
            return None

        url_canonica = canonical_url(enlace)
        titulo = normalize_text(entry.get("title") or "")

        contenido = entry.get("content")
        valor = contenido[0].get("value", "") if contenido else ""
        if valor.strip():
            html, metodo, calidad = valor, "feed_content", "completa"
        else:
            html, metodo, calidad = entry.get("summary") or "", "feed_summary", "solo_resumen"
        texto = html_a_texto(html) if html else ""

        base_hash = texto if texto else f"{titulo}\n{url_canonica}"

        return RawItem(
            item_id=make_item_id(self._fuente.id, url_canonica),
            fuente_id=self._fuente.id,
            tipo_fuente="rss",
            url_original=enlace,
            url_canonica=url_canonica,
            guid=guid,
            titulo=titulo,
            autor=entry.get("author") or None,
            fecha_publicacion=_fecha_entrada(entry),
            fecha_captura=ahora,
            idioma=idioma,
            texto=texto,
            hash_contenido=content_hash(base_hash),
            metodo_extraccion=metodo,
            calidad_extraccion=calidad,
            estado="nuevo",
        )

# core/ingestion/web_collector.py
"""Colector web para fuentes sin feed (documento v2, secciones 4.6 y 4.8).

Estrategia: descargar la página de listado, extraer los enlaces que coinciden
con `patron_enlaces`, descargar cada artículo aún no visto y extraer su texto
principal con Trafilatura (sin selectores CSS por sitio).

Decisiones mías que el documento no precisa:
- Solo se siguen enlaces del mismo host que el listado.
- Los artículos se descargan de uno en uno, con una pausa entre peticiones, y
  se procesan como máximo MAX_ARTICULOS_POR_EJECUCION por ejecución.
- "Ya visto" significa que la URL canónica ya está guardada para la fuente
  (lo decide `ya_visto`, que aporta el runner). Un artículo ya guardado no se
  vuelve a descargar, así que un cambio posterior en su texto no se detecta.
- robots.txt (RFC 9309): 4xx = se permite todo; 5xx o error de red = se
  prohíbe todo; si el listado está prohibido, la fuente falla con un error.
- Si algún artículo falla, o quedan artículos sin procesar por el tope, el
  resultado devuelve etag=None y last_modified=None. Así el runner conserva los
  validadores anteriores y el listado se vuelve a descargar en la siguiente
  ejecución en lugar de responder 304.
- Un artículo sin texto extraíble se omite (no se guarda), así que se vuelve a
  intentar cuando el listado cambie. Texto corto sí se guarda, como `parcial`.

No implementado: render_js (Playwright), límite de peticiones por dominio.
"""
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Awaitable, Callable, Optional
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from config import settings
from core.ingestion.base import (
    BaseCollector,
    ErrorColector,
    ResultadoRecoleccion,
    Validadores,
)
from core.ingestion.http_client import ErrorDescarga, ErrorHttp, HttpClient
from core.ingestion.normalizer import (
    canonical_url,
    content_hash,
    make_item_id,
    normalize_text,
    to_utc,
)
from core.models.raw_item import RawItem
from core.models.source_config import Defaults, Fuente


class AccesoProhibidoPorRobots(ErrorColector):
    """robots.txt no permite (o no se pudo comprobar) el acceso al listado."""


class RenderJsNoDisponible(ErrorColector):
    """La fuente pide render_js, que todavía no está implementado."""


@dataclass
class Extraccion:
    texto: str
    titulo: str = ""
    autor: Optional[str] = None
    fecha: Optional[datetime] = None
    idioma: Optional[str] = None


# ------------------------------------------------------------------ extracción
def extraer_con_trafilatura(html: str) -> Extraccion:
    import trafilatura  # import perezoso: solo hace falta con el extractor real

    texto = (
        trafilatura.extract(
            html, include_comments=False, include_tables=False, favor_precision=True
        )
        or ""
    )
    meta = trafilatura.extract_metadata(html)
    fecha = None
    fecha_txt = getattr(meta, "date", None)
    if fecha_txt:
        try:
            fecha = datetime.strptime(str(fecha_txt)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            fecha = None
    return Extraccion(
        texto=texto,
        titulo=getattr(meta, "title", None) or "",
        autor=getattr(meta, "author", None) or None,
        fecha=fecha,
        idioma=getattr(meta, "language", None) or None,
    )


_RE_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def decodificar_html(contenido: bytes) -> str:
    """Decodifica según <meta charset> si existe; si no, UTF-8 tolerante."""
    m = _RE_CHARSET.search(contenido[:4096])
    if m:
        try:
            return contenido.decode(m.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    return contenido.decode("utf-8", errors="replace")


class _ExtractorEnlaces(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for nombre, valor in attrs:
                if nombre == "href" and valor:
                    self.hrefs.append(valor)


def extraer_enlaces(html: str, base_url: str, patron: str) -> list[tuple[str, str]]:
    """Devuelve (url_absoluta, url_canonica) de los enlaces del mismo host que
    coinciden con `patron`, sin repetidos y sin la propia página de listado."""
    extractor = _ExtractorEnlaces()
    extractor.feed(html)
    extractor.close()

    regex = re.compile(patron)
    host_base = (urlsplit(base_url).hostname or "").lower()
    canonica_base = canonical_url(base_url)
    vistos: set[str] = set()
    resultado: list[tuple[str, str]] = []
    for href in extractor.hrefs:
        absoluta, _ = urldefrag(urljoin(base_url, href.strip()))
        partes = urlsplit(absoluta)
        if partes.scheme not in ("http", "https"):
            continue
        if (partes.hostname or "").lower() != host_base:
            continue
        if not regex.search(absoluta):
            continue
        canonica = canonical_url(absoluta)
        if canonica == canonica_base or canonica in vistos:
            continue
        vistos.add(canonica)
        resultado.append((absoluta, canonica))
    return resultado


# ---------------------------------------------------------------- robots.txt
def _token_agente(user_agent: str) -> str:
    return re.split(r"[/\s]", user_agent.strip())[0] or "*"


class _Robots:
    """Consulta robots.txt una vez por origen y recuerda el resultado."""

    def __init__(self, cliente: HttpClient, user_agent: str):
        self._cliente = cliente
        self._agente = _token_agente(user_agent)
        self._cache: dict[str, tuple[Callable[[str], bool], Optional[str]]] = {}

    async def permitido(self, url: str) -> tuple[bool, Optional[str]]:
        partes = urlsplit(url)
        origen = f"{partes.scheme}://{partes.netloc}"
        if origen not in self._cache:
            self._cache[origen] = await self._cargar(origen)
        regla, motivo = self._cache[origen]
        return regla(url), motivo

    async def _cargar(self, origen: str) -> tuple[Callable[[str], bool], Optional[str]]:
        try:
            resp = await self._cliente.obtener(f"{origen}/robots.txt")
        except ErrorHttp as e:
            if 400 <= e.status < 500:
                return (lambda url: True), None
            return (lambda url: False), f"robots.txt no disponible (HTTP {e.status})"
        except ErrorDescarga as e:
            return (lambda url: False), f"robots.txt no disponible ({type(e).__name__})"

        parser = RobotFileParser()
        parser.parse(resp.contenido.decode("utf-8", errors="replace").splitlines())
        parser.modified()
        return (lambda url: parser.can_fetch(self._agente, url)), None


# ------------------------------------------------------------------- colector
class WebCollector(BaseCollector):
    tipo = "web"

    def __init__(
        self,
        fuente: Fuente,
        cliente: HttpClient,
        *,
        ahora: Optional[Callable[[], datetime]] = None,
        defaults: Optional[Defaults] = None,
        ya_visto: Optional[Callable[[str, str], bool]] = None,
        extractor: Callable[[str], Extraccion] = extraer_con_trafilatura,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_articulos: int = settings.MAX_ARTICULOS_POR_EJECUCION,
        pausa_segundos: float = settings.PAUSA_ENTRE_ARTICULOS_SEGUNDOS,
    ):
        super().__init__(fuente, cliente, ahora=ahora, defaults=defaults)
        if defaults is None:
            raise ValueError(
                "WebCollector necesita los `defaults` de sources.yaml "
                "(user_agent y respetar_robots_txt)"
            )
        self._ya_visto = ya_visto or (lambda fuente_id, url_canonica: False)
        self._extractor = extractor
        self._dormir = dormir
        self._max_articulos = max_articulos
        self._pausa = pausa_segundos

    async def recolectar(
        self, validadores: Optional[Validadores] = None
    ) -> ResultadoRecoleccion:
        web = self._fuente.web
        if web.render_js:
            raise RenderJsNoDisponible(
                f"'{self._fuente.id}' pide render_js, que requiere Playwright y aún no está integrado"
            )

        previo = validadores or Validadores()
        robots = (
            _Robots(self._cliente, self._defaults.user_agent)
            if self._defaults.respetar_robots_txt
            else None
        )

        url_listado = str(self._fuente.url)
        if robots is not None:
            permitido, motivo = await robots.permitido(url_listado)
            if not permitido:
                raise AccesoProhibidoPorRobots(
                    motivo or f"robots.txt no permite acceder a {url_listado}"
                )

        resp = await self._descargar(validadores)
        if resp.no_modificado:
            return ResultadoRecoleccion(
                fuente_id=self._fuente.id,
                status=304,
                etag=resp.etag or previo.etag,
                last_modified=resp.last_modified or previo.last_modified,
            )

        advertencias: list[str] = []
        enlaces = extraer_enlaces(
            decodificar_html(resp.contenido), resp.url_final, web.patron_enlaces
        )
        if not enlaces:
            advertencias.append(
                "El listado no contiene enlaces que coincidan con patron_enlaces"
            )

        pendientes = [(u, c) for u, c in enlaces if not self._ya_visto(self._fuente.id, c)]
        truncado = len(pendientes) > self._max_articulos
        if truncado:
            advertencias.append(
                f"{len(pendientes)} artículos nuevos; se procesan {self._max_articulos} "
                "y el resto queda para la siguiente ejecución"
            )
            pendientes = pendientes[: self._max_articulos]

        ahora = self._ahora()
        items: list[RawItem] = []
        bloqueados = 0
        sin_texto = 0
        fallos: list[str] = []
        for indice, (url, canonica) in enumerate(pendientes):
            if indice and self._pausa:
                await self._dormir(self._pausa)
            if robots is not None:
                permitido, _ = await robots.permitido(canonica)
                if not permitido:
                    bloqueados += 1
                    continue
            try:
                articulo = await self._cliente.obtener(canonica)
                extraccion = self._extractor(decodificar_html(articulo.contenido))
            except ErrorDescarga as e:
                fallos.append(f"{canonica} ({type(e).__name__})")
                continue
            except Exception as e:  # el extractor puede fallar con HTML raro
                fallos.append(f"{canonica} (extracción: {type(e).__name__})")
                continue

            texto = normalize_text(extraccion.texto)
            if not texto:
                sin_texto += 1
                continue
            items.append(self._a_item(url, canonica, texto, extraccion, ahora))

        if bloqueados:
            advertencias.append(f"{bloqueados} artículos omitidos por robots.txt")
        if sin_texto:
            advertencias.append(f"{sin_texto} artículos sin texto extraíble")
        if fallos:
            advertencias.append(
                f"{len(fallos)} artículos fallaron: " + "; ".join(fallos[:3])
            )

        incompleto = truncado or bool(fallos)
        return ResultadoRecoleccion(
            fuente_id=self._fuente.id,
            status=resp.status,
            items=items,
            etag=None if incompleto else resp.etag,
            last_modified=None if incompleto else resp.last_modified,
            omitidos=bloqueados + sin_texto + len(fallos),
            advertencias=advertencias,
        )

    def _a_item(
        self,
        url: str,
        canonica: str,
        texto: str,
        extraccion: Extraccion,
        ahora: datetime,
    ) -> RawItem:
        calidad = (
            "completa"
            if len(texto) >= settings.MIN_CARACTERES_TEXTO_COMPLETO
            else "parcial"
        )
        return RawItem(
            item_id=make_item_id(self._fuente.id, canonica),
            fuente_id=self._fuente.id,
            tipo_fuente="web",
            url_original=url,
            url_canonica=canonica,
            guid=None,
            titulo=normalize_text(extraccion.titulo),
            autor=extraccion.autor or None,
            fecha_publicacion=to_utc(extraccion.fecha),
            fecha_captura=ahora,
            idioma=extraccion.idioma,
            texto=texto,
            hash_contenido=content_hash(texto),
            metodo_extraccion="trafilatura",
            calidad_extraccion=calidad,
            estado="nuevo",
        )

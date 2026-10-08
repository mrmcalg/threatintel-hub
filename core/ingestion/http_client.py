# core/ingestion/http_client.py
"""Cliente HTTP de la ingesta (documento v2, secciones 4.6 y 4.9).

Incluye: timeouts, límite de tamaño, reintentos con backoff exponencial y
jitter, respeto de Retry-After, peticiones condicionales (ETag /
If-Modified-Since) y validación anti-SSRF en cada salto de redirección.

Limitaciones conocidas (no resueltas aquí):
- Entre la validación DNS y la conexión real, httpx vuelve a resolver el host
  (ventana de "DNS rebinding"). Cerrarla exige fijar la IP validada.
- No consulta robots.txt ni limita la concurrencia por dominio.
- httpx lee variables de proxy del entorno por defecto; con proxy, el destino
  final lo resuelve el proxy y no esta validación.
"""
import asyncio
import ipaddress
import random
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Optional

import httpx

from config import settings
from core.models.source_config import Defaults


# ------------------------------------------------------------------ errores
class ErrorDescarga(Exception):
    """Base de los errores de descarga."""


class UrlNoPermitida(ErrorDescarga):
    """Esquema inválido o destino no público (protección SSRF)."""


class RespuestaDemasiadoGrande(ErrorDescarga):
    """La respuesta supera el máximo de bytes configurado."""


class DemasiadasRedirecciones(ErrorDescarga):
    """Se superó el número máximo de redirecciones."""


class ErrorDeRed(ErrorDescarga):
    """Fallo de transporte (timeout, conexión, DNS) tras agotar reintentos."""


class ErrorHttp(ErrorDescarga):
    def __init__(self, status: int, url: str, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status} en {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after


# ---------------------------------------------------------------- utilidades
@dataclass
class Respuesta:
    status: int
    contenido: bytes
    url_final: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    retry_after: Optional[float] = None

    @property
    def no_modificado(self) -> bool:
        return self.status == 304


def _ip_no_publica(texto: str) -> bool:
    """True si la IP no es de Internet público: privada, loopback, link-local
    (incluye metadatos cloud 169.254.169.254), CGNAT, documentación, etc."""
    ip = ipaddress.ip_address(texto)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def _parse_retry_after(valor: Optional[str]) -> Optional[float]:
    if valor is None:
        return None
    v = valor.strip()
    if v.isdigit():
        return float(v)
    try:
        fecha = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return max(0.0, (fecha - datetime.now(timezone.utc)).total_seconds())


async def resolver_dns(host: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise ErrorDeRed(f"No se pudo resolver {host}: {e}") from e
    return sorted({info[4][0] for info in infos})


_REDIRECCIONES = (301, 302, 303, 307, 308)


# ------------------------------------------------------------------- cliente
class HttpClient:
    def __init__(
        self,
        *,
        timeout_segundos: float,
        max_bytes: int,
        reintentos: int,
        user_agent: str,
        backoff_base: float = settings.BACKOFF_BASE_SEGUNDOS,
        backoff_max: float = settings.BACKOFF_MAX_SEGUNDOS,
        max_redirecciones: int = settings.MAX_REDIRECCIONES,
        resolver: Callable[[str], Awaitable[list[str]]] = resolver_dns,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._timeout = timeout_segundos
        self._max_bytes = max_bytes
        self._reintentos = reintentos
        self._user_agent = user_agent
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_redirecciones = max_redirecciones
        self._resolver = resolver
        self._dormir = dormir

    @classmethod
    def desde_defaults(cls, defaults: Defaults, **kwargs) -> "HttpClient":
        """Construye el cliente con el bloque `defaults` de sources.yaml."""
        return cls(
            timeout_segundos=defaults.timeout_segundos,
            max_bytes=defaults.max_bytes,
            reintentos=defaults.reintentos,
            user_agent=defaults.user_agent,
            **kwargs,
        )

    async def obtener(
        self,
        url: str,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> Respuesta:
        cabeceras = {"User-Agent": self._user_agent}
        if etag:
            cabeceras["If-None-Match"] = etag
        if last_modified:
            cabeceras["If-Modified-Since"] = last_modified

        ultimo_error: Optional[Exception] = None
        for intento in range(self._reintentos + 1):
            espera: Optional[float] = None
            try:
                resp = await self._una_peticion(url, cabeceras)
            except (httpx.TransportError, ErrorDeRed) as e:
                ultimo_error = e
            else:
                if 200 <= resp.status < 300 or resp.status == 304:
                    return resp
                if resp.status not in settings.CODIGOS_REINTENTABLES:
                    raise ErrorHttp(resp.status, url)
                if resp.retry_after is not None and resp.retry_after > self._backoff_max:
                    # El servidor pide esperar más de lo razonable: no insistimos
                    # y dejamos el dato para el circuit breaker.
                    raise ErrorHttp(resp.status, url, resp.retry_after)
                ultimo_error = ErrorHttp(resp.status, url, resp.retry_after)
                espera = resp.retry_after

            if intento == self._reintentos:
                break
            if espera is None:
                espera = self._calcular_backoff(intento)
            await self._dormir(espera)

        if isinstance(ultimo_error, ErrorHttp):
            raise ultimo_error
        raise ErrorDeRed(f"Fallo de red al consultar {url}: {ultimo_error}") from ultimo_error

    def _calcular_backoff(self, intento: int) -> float:
        tope = min(self._backoff_max, self._backoff_base * (2**intento))
        return random.uniform(0, tope)

    async def _validar_destino(self, url: str) -> None:
        try:
            u = httpx.URL(url)
        except httpx.InvalidURL as e:
            raise UrlNoPermitida(f"URL inválida: {e}") from e
        if u.scheme not in ("http", "https"):
            raise UrlNoPermitida(f"Esquema no permitido: {u.scheme!r}")
        host = u.host
        if not host:
            raise UrlNoPermitida("La URL no tiene host")
        try:
            ips = [str(ipaddress.ip_address(host))]
        except ValueError:
            ips = await self._resolver(host)
        if not ips:
            raise ErrorDeRed(f"No se pudo resolver {host}")
        for ip in ips:
            if _ip_no_publica(ip):
                raise UrlNoPermitida(f"Destino no público: {host} -> {ip}")

    async def _una_peticion(self, url: str, cabeceras: dict) -> Respuesta:
        url_actual = url
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as cliente:
            for _ in range(self._max_redirecciones + 1):
                await self._validar_destino(url_actual)
                async with cliente.stream("GET", url_actual, headers=cabeceras) as r:
                    if r.status_code in _REDIRECCIONES and "location" in r.headers:
                        url_actual = str(httpx.URL(url_actual).join(r.headers["location"]))
                        continue
                    return await self._leer(r, url_actual)
        raise DemasiadasRedirecciones(
            f"Más de {self._max_redirecciones} redirecciones desde {url}"
        )

    async def _leer(self, r: httpx.Response, url: str) -> Respuesta:
        declarado = r.headers.get("content-length")
        if declarado and declarado.isdigit() and int(declarado) > self._max_bytes:
            raise RespuestaDemasiadoGrande(
                f"Content-Length {declarado} supera el máximo de {self._max_bytes} bytes"
            )
        cuerpo = b""
        if 200 <= r.status_code < 300:
            partes: list[bytes] = []
            total = 0
            async for trozo in r.aiter_bytes():
                total += len(trozo)
                if total > self._max_bytes:
                    raise RespuestaDemasiadoGrande(
                        f"La respuesta supera el máximo de {self._max_bytes} bytes"
                    )
                partes.append(trozo)
            cuerpo = b"".join(partes)
        return Respuesta(
            status=r.status_code,
            contenido=cuerpo,
            url_final=url,
            etag=r.headers.get("etag"),
            last_modified=r.headers.get("last-modified"),
            retry_after=_parse_retry_after(r.headers.get("retry-after")),
        )

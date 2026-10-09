# core/ingestion/base.py
"""Interfaz común de los colectores (documento v2, sección 4.4).

Un colector descarga una fuente y devuelve ítems que cumplen el contrato
RawItem. No persiste nada: guardar ítems (dedup.py) y guardar ETag /
Last-Modified (estado de la fuente) lo hace quien orquesta la ingesta.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, ClassVar, Optional

from core.ingestion.http_client import HttpClient, Respuesta
from core.models.raw_item import RawItem
from core.models.source_config import Defaults, Fuente


class ErrorColector(Exception):
    """Base de los errores propios de los colectores."""


class FeedInvalido(ErrorColector):
    """El contenido descargado no es un feed reconocible."""


class ContenidoNoSeguro(ErrorColector):
    """El XML contiene construcciones prohibidas (entidades, XXE)."""


@dataclass(frozen=True)
class Validadores:
    etag: Optional[str] = None
    last_modified: Optional[str] = None


@dataclass
class ResultadoRecoleccion:
    fuente_id: str
    status: int
    items: list[RawItem] = field(default_factory=list)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    omitidos: int = 0
    advertencias: list[str] = field(default_factory=list)

    @property
    def no_modificado(self) -> bool:
        return self.status == 304


class BaseCollector(ABC):
    tipo: ClassVar[str]

    def __init__(
        self,
        fuente: Fuente,
        cliente: HttpClient,
        *,
        ahora: Optional[Callable[[], datetime]] = None,
        defaults: Optional[Defaults] = None,
    ):
        if fuente.tipo != self.tipo:
            raise ValueError(
                f"{type(self).__name__} solo acepta fuentes de tipo '{self.tipo}', "
                f"y '{fuente.id}' es de tipo '{fuente.tipo}'"
            )
        self._fuente = fuente
        self._cliente = cliente
        self._defaults = defaults
        self._ahora = ahora or (lambda: datetime.now(timezone.utc))

    async def _descargar(self, validadores: Optional[Validadores]) -> Respuesta:
        v = validadores or Validadores()
        return await self._cliente.obtener(
            str(self._fuente.url), etag=v.etag, last_modified=v.last_modified
        )

    @abstractmethod
    async def recolectar(
        self, validadores: Optional[Validadores] = None
    ) -> ResultadoRecoleccion:
        """Descarga la fuente y devuelve sus ítems normalizados."""

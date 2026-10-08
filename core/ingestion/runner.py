# core/ingestion/runner.py
"""Orquestador de la ingesta (documento v2, secciones 4.6, 4.7 y 4.10).

Ejecución:  python -m core.ingestion.runner [--solo ID [ID ...]]

Qué hace por cada fuente activa: lee sus validadores (ETag / Last-Modified)
de la BD, ejecuta su colector, clasifica y guarda los ítems, registra la
ejecución y actualiza el estado de la fuente. Un fallo en una fuente se
registra y nunca detiene a las demás.

Alcance actual (lo que NO hace todavía):
- No decide cuándo toca ejecutar cada fuente según `frecuencia_minutos`
  (eso es scheduler.py, Sprint 1b): ejecuta todas las activas cuando se invoca.
- El estado "activo" sale solo del YAML; el override de la consola en la BD
  (documento v2, sección 4.2) llegará con config_sync.py.
- No hay circuit breaker: se cuentan los fallos consecutivos (health.py los
  usará), pero ninguna fuente se bloquea.
- Las fuentes de tipo `web` se omiten: aún no hay colector.
- No consulta robots.txt y no limita la concurrencia por dominio.
- Toda fuente de tipo `api` se procesa con KevCollector, que solo conoce el
  formato del catálogo CISA KEV.
"""
import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from config import settings
from core.ingestion.api_collector import KevCollector
from core.ingestion.base import BaseCollector, ErrorColector, Validadores
from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import ErrorDescarga, ErrorHttp, HttpClient
from core.ingestion.rss_collector import RssCollector
from core.models.source_config import ConfigError, Fuente, SourcesFile, load_sources

log = logging.getLogger("ingesta")

COLECTORES: dict[str, type[BaseCollector]] = {"rss": RssCollector, "api": KevCollector}


@dataclass
class ResumenFuente:
    fuente_id: str
    resultado: Literal["ok", "sin_cambios", "error", "omitida", "pausada"]
    nuevos: int = 0
    actualizados: int = 0
    duplicados: int = 0
    omitidos_item: int = 0
    advertencias: list[str] = field(default_factory=list)
    detalle: Optional[str] = None
    duracion_s: float = 0.0


class IngestionRunner:
    def __init__(
        self,
        config: SourcesFile,
        store: ItemStore,
        cliente: HttpClient,
        *,
        max_concurrentes: int = settings.MAX_FUENTES_CONCURRENTES,
        ahora: Optional[Callable[[], datetime]] = None,
    ):
        self._config = config
        self._store = store
        self._cliente = cliente
        self._max_concurrentes = max_concurrentes
        self._ahora = ahora or (lambda: datetime.now(timezone.utc))

    async def ejecutar(self, solo: Optional[set[str]] = None) -> list[ResumenFuente]:
        fuentes = self._config.fuentes
        if solo is not None:
            desconocidas = solo - {f.id for f in fuentes}
            if desconocidas:
                raise ValueError(f"Fuentes desconocidas: {sorted(desconocidas)}")
            fuentes = [f for f in fuentes if f.id in solo]

        semaforo = asyncio.Semaphore(self._max_concurrentes)

        async def con_limite(fuente: Fuente) -> ResumenFuente:
            async with semaforo:
                return await self._ejecutar_fuente(fuente)

        return list(await asyncio.gather(*(con_limite(f) for f in fuentes)))

    async def _ejecutar_fuente(self, fuente: Fuente) -> ResumenFuente:
        if not fuente.activo:
            return ResumenFuente(fuente.id, "pausada")
        clase = COLECTORES.get(fuente.tipo)
        if clase is None:
            return ResumenFuente(
                fuente.id, "omitida", detalle=f"tipo '{fuente.tipo}' sin colector todavía"
            )

        inicio = self._ahora()
        t0 = time.monotonic()
        http_status: Optional[int] = None
        try:
            estado = self._store.obtener_estado_fuente(fuente.id)
            colector = clase(fuente, self._cliente, ahora=self._ahora)
            resultado = await colector.recolectar(Validadores(estado.etag, estado.last_modified))
            http_status = resultado.status
            clasificados = self._store.registrar_lote(resultado.items)
        except Exception as e:  # aislamiento de fallos: una fuente nunca detiene a las demás
            if isinstance(e, ErrorHttp):
                http_status = e.status
            detalle = f"{type(e).__name__}: {e}"
            if not isinstance(e, (ErrorDescarga, ErrorColector)):
                log.exception("Fallo inesperado en la fuente %s", fuente.id)
            self._store.registrar_ejecucion(
                fuente.id, inicio, self._ahora(), http_status, 0, 0, 0, detalle
            )
            self._store.registrar_fallo_fuente(fuente.id, detalle)
            return ResumenFuente(
                fuente.id, "error", detalle=detalle, duracion_s=time.monotonic() - t0
            )

        conteo = Counter(i.estado for i in clasificados)
        self._store.registrar_ejecucion(
            fuente.id,
            inicio,
            self._ahora(),
            resultado.status,
            conteo["nuevo"],
            conteo["actualizado"],
            conteo["duplicado"],
            None,
        )
        self._store.registrar_exito_fuente(fuente.id, resultado.etag, resultado.last_modified)
        return ResumenFuente(
            fuente.id,
            "sin_cambios" if resultado.no_modificado else "ok",
            nuevos=conteo["nuevo"],
            actualizados=conteo["actualizado"],
            duplicados=conteo["duplicado"],
            omitidos_item=resultado.omitidos,
            advertencias=resultado.advertencias,
            duracion_s=time.monotonic() - t0,
        )


def _imprimir(resumenes: list[ResumenFuente]) -> None:
    print(f"\n{'fuente':<26}{'resultado':<13}{'nuevos':>7}{'actual.':>8}{'dupl.':>7}{'seg':>6}  detalle")
    for r in resumenes:
        print(
            f"{r.fuente_id:<26}{r.resultado:<13}{r.nuevos:>7}{r.actualizados:>8}"
            f"{r.duplicados:>7}{r.duracion_s:>6.1f}  {r.detalle or ''}"
        )
        for aviso in r.advertencias:
            print(f"{'':<26}  aviso: {aviso}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ejecuta la ingesta de ThreatIntel Hub")
    parser.add_argument("--solo", nargs="+", metavar="ID", help="ids de fuentes a ejecutar")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        config = load_sources(settings.RUTA_SOURCES)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    store = ItemStore(settings.RUTA_BD)
    try:
        runner = IngestionRunner(config, store, HttpClient.desde_defaults(config.defaults))
        try:
            resumenes = asyncio.run(runner.ejecutar(set(args.solo) if args.solo else None))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    finally:
        store.cerrar()

    _imprimir(resumenes)
    return 1 if any(r.resultado == "error" for r in resumenes) else 0


if __name__ == "__main__":
    raise SystemExit(main())

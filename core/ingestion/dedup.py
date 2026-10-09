# core/ingestion/dedup.py
"""Deduplicación de ítems en la ingesta y persistencia mínima en SQLite.

Orden de comprobación (documento v2, sección 4.5; la primera coincidencia gana):
  1. guid + fuente_id
  2. url_canonica
  3. hash_contenido

Interpretación mía (el documento no lo precisa): las tres comprobaciones se
hacen dentro de la misma fuente. Agrupar artículos de fuentes distintas sobre
el mismo incidente es trabajo de la Fase 2 (clustering).

También guarda el estado operativo de cada fuente (ETag, Last-Modified, último
éxito, fallos consecutivos), que el documento ubica en la BD (sección 4.2).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from core.models.raw_item import RawItem

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id        TEXT PRIMARY KEY,
    fuente_id      TEXT NOT NULL,
    guid           TEXT,
    url_canonica   TEXT NOT NULL,
    hash_contenido TEXT NOT NULL,
    primera_vez    TEXT NOT NULL,
    ultima_vez     TEXT NOT NULL,
    datos_json     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_guid
    ON items (fuente_id, guid) WHERE guid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_url
    ON items (fuente_id, url_canonica);
CREATE INDEX IF NOT EXISTS ix_items_hash
    ON items (fuente_id, hash_contenido);

CREATE TABLE IF NOT EXISTS ejecuciones (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente_id    TEXT NOT NULL,
    inicio       TEXT NOT NULL,
    fin          TEXT NOT NULL,
    http_status  INTEGER,
    nuevos       INTEGER NOT NULL,
    actualizados INTEGER NOT NULL,
    duplicados   INTEGER NOT NULL,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS estado_fuentes (
    fuente_id           TEXT PRIMARY KEY,
    etag                TEXT,
    last_modified       TEXT,
    ultimo_exito        TEXT,
    ultimo_intento      TEXT,
    fallos_consecutivos INTEGER NOT NULL DEFAULT 0,
    ultimo_error        TEXT
);
"""

_MAX_ERROR = 500


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EstadoFuente:
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    ultimo_exito: Optional[str] = None
    ultimo_intento: Optional[str] = None
    fallos_consecutivos: int = 0
    ultimo_error: Optional[str] = None


class ItemStore:
    def __init__(self, db_path: Union[str, Path]):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_ESQUEMA)

    def __enter__(self) -> "ItemStore":
        return self

    def __exit__(self, *exc) -> None:
        self.cerrar()

    def cerrar(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ ítems
    def registrar(self, item: RawItem) -> RawItem:
        """Clasifica el ítem (nuevo / actualizado / duplicado), lo persiste y
        devuelve una copia con `estado` e `item_id` definitivos."""
        with self._conn:
            return self._registrar_en_transaccion(item, _ahora())

    def registrar_lote(self, items: list[RawItem]) -> list[RawItem]:
        """Igual que `registrar`, pero todo el lote en UNA transacción: es
        mucho más rápido en disco y, si algo falla, no queda un lote a medias."""
        ahora = _ahora()
        with self._conn:
            return [self._registrar_en_transaccion(i, ahora) for i in items]

    def _registrar_en_transaccion(self, item: RawItem, ahora: str) -> RawItem:
        fila = None
        if item.guid:
            fila = self._conn.execute(
                "SELECT item_id, hash_contenido FROM items WHERE fuente_id = ? AND guid = ?",
                (item.fuente_id, item.guid),
            ).fetchone()
        if fila is None:
            fila = self._conn.execute(
                "SELECT item_id, hash_contenido FROM items WHERE fuente_id = ? AND url_canonica = ?",
                (item.fuente_id, item.url_canonica),
            ).fetchone()

        if fila is not None:
            if fila["hash_contenido"] == item.hash_contenido:
                self._conn.execute(
                    "UPDATE items SET ultima_vez = ? WHERE item_id = ?",
                    (ahora, fila["item_id"]),
                )
                return item.model_copy(update={"estado": "duplicado", "item_id": fila["item_id"]})
            resultado = item.model_copy(update={"estado": "actualizado", "item_id": fila["item_id"]})
            self._conn.execute(
                "UPDATE items SET hash_contenido = ?, ultima_vez = ?, datos_json = ? WHERE item_id = ?",
                (item.hash_contenido, ahora, resultado.model_dump_json(), fila["item_id"]),
            )
            return resultado

        por_hash = self._conn.execute(
            "SELECT item_id FROM items WHERE fuente_id = ? AND hash_contenido = ? LIMIT 1",
            (item.fuente_id, item.hash_contenido),
        ).fetchone()
        if por_hash is not None:
            self._conn.execute(
                "UPDATE items SET ultima_vez = ? WHERE item_id = ?",
                (ahora, por_hash["item_id"]),
            )
            return item.model_copy(update={"estado": "duplicado", "item_id": por_hash["item_id"]})

        resultado = item.model_copy(update={"estado": "nuevo"})
        self._conn.execute(
            "INSERT INTO items (item_id, fuente_id, guid, url_canonica, hash_contenido,"
            " primera_vez, ultima_vez, datos_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resultado.item_id,
                resultado.fuente_id,
                resultado.guid,
                resultado.url_canonica,
                resultado.hash_contenido,
                ahora,
                ahora,
                resultado.model_dump_json(),
            ),
        )
        return resultado

    def contar_items(self, fuente_id: Optional[str] = None) -> int:
        if fuente_id is None:
            fila = self._conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
        else:
            fila = self._conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE fuente_id = ?", (fuente_id,)
            ).fetchone()
        return fila["n"]

    def existe_url(self, fuente_id: str, url_canonica: str) -> bool:
        """True si la fuente ya tiene guardada esa URL canónica."""
        fila = self._conn.execute(
            "SELECT 1 FROM items WHERE fuente_id = ? AND url_canonica = ? LIMIT 1",
            (fuente_id, url_canonica),
        ).fetchone()
        return fila is not None

    def listar_items(self, fuente_id: str, limite: int = 10) -> list[RawItem]:
        """Los últimos ítems insertados de una fuente (para inspección)."""
        filas = self._conn.execute(
            "SELECT datos_json FROM items WHERE fuente_id = ? ORDER BY rowid DESC LIMIT ?",
            (fuente_id, limite),
        ).fetchall()
        return [RawItem.model_validate_json(f["datos_json"]) for f in filas]

    # ------------------------------------------------------------- ejecuciones
    def registrar_ejecucion(
        self,
        fuente_id: str,
        inicio: datetime,
        fin: datetime,
        http_status: Optional[int],
        nuevos: int,
        actualizados: int,
        duplicados: int,
        error: Optional[str] = None,
    ) -> None:
        """Una fila por ejecución de una fuente (documento v2, sección 4.7)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO ejecuciones (fuente_id, inicio, fin, http_status, nuevos,"
                " actualizados, duplicados, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fuente_id,
                    inicio.isoformat(),
                    fin.isoformat(),
                    http_status,
                    nuevos,
                    actualizados,
                    duplicados,
                    error,
                ),
            )

    def listar_ejecuciones(self, fuente_id: str) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM ejecuciones WHERE fuente_id = ? ORDER BY id", (fuente_id,)
        ).fetchall()
        return [dict(f) for f in filas]

    # ------------------------------------------------------ estado de fuentes
    def obtener_estado_fuente(self, fuente_id: str) -> EstadoFuente:
        fila = self._conn.execute(
            "SELECT etag, last_modified, ultimo_exito, ultimo_intento,"
            " fallos_consecutivos, ultimo_error FROM estado_fuentes WHERE fuente_id = ?",
            (fuente_id,),
        ).fetchone()
        return EstadoFuente(**dict(fila)) if fila is not None else EstadoFuente()

    def registrar_exito_fuente(
        self, fuente_id: str, etag: Optional[str], last_modified: Optional[str]
    ) -> None:
        """Guarda los validadores y reinicia los fallos. Si el servidor no
        envió ETag / Last-Modified (p. ej. un 304), se conservan los previos."""
        ahora = _ahora()
        with self._conn:
            self._conn.execute(
                "INSERT INTO estado_fuentes (fuente_id, etag, last_modified, ultimo_exito,"
                " ultimo_intento, fallos_consecutivos, ultimo_error)"
                " VALUES (?, ?, ?, ?, ?, 0, NULL)"
                " ON CONFLICT(fuente_id) DO UPDATE SET"
                " etag = COALESCE(excluded.etag, etag),"
                " last_modified = COALESCE(excluded.last_modified, last_modified),"
                " ultimo_exito = excluded.ultimo_exito,"
                " ultimo_intento = excluded.ultimo_intento,"
                " fallos_consecutivos = 0, ultimo_error = NULL",
                (fuente_id, etag, last_modified, ahora, ahora),
            )

    def registrar_fallo_fuente(self, fuente_id: str, error: str) -> None:
        ahora = _ahora()
        with self._conn:
            self._conn.execute(
                "INSERT INTO estado_fuentes (fuente_id, ultimo_intento, fallos_consecutivos,"
                " ultimo_error) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(fuente_id) DO UPDATE SET"
                " ultimo_intento = excluded.ultimo_intento,"
                " fallos_consecutivos = fallos_consecutivos + 1,"
                " ultimo_error = excluded.ultimo_error",
                (fuente_id, ahora, error[:_MAX_ERROR]),
            )

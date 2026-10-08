# core/ingestion/dedup.py
"""Deduplicación de ítems en la ingesta y persistencia mínima en SQLite.

Orden de comprobación (documento v2, sección 4.5; la primera coincidencia gana):
  1. guid + fuente_id
  2. url_canonica
  3. hash_contenido

Interpretación mía (el documento no lo precisa): las tres comprobaciones se
hacen dentro de la misma fuente. Agrupar artículos de fuentes distintas sobre
el mismo incidente es trabajo de la Fase 2 (clustering).
"""
import sqlite3
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
"""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        ahora = _ahora()
        with self._conn:
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

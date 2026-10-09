# core/ingestion/inspeccionar.py
"""Muestra ítems ya guardados de una fuente, para revisarlos a ojo.

Uso:  python -m core.ingestion.inspeccionar --fuente ID [--n 3] [--texto 300]
"""
import argparse
import sys
from typing import Optional

from config import settings
from core.ingestion.dedup import ItemStore


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Muestra ítems guardados de una fuente")
    parser.add_argument("--fuente", required=True, help="id de la fuente (ver config/sources.yaml)")
    parser.add_argument("--n", type=int, default=3, help="cuántos ítems mostrar")
    parser.add_argument("--texto", type=int, default=300, help="caracteres del texto a mostrar")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not settings.RUTA_BD.exists():
        print("No existe la base de datos. Ejecuta primero: python -m core.ingestion.runner")
        return 1

    store = ItemStore(settings.RUTA_BD)
    try:
        items = store.listar_items(args.fuente, args.n)
        total = store.contar_items(args.fuente)
    finally:
        store.cerrar()

    if not items:
        print(
            f"No hay ítems guardados de '{args.fuente}'. "
            f"Ejecuta: python -m core.ingestion.runner --solo {args.fuente}"
        )
        return 1

    print(f"{args.fuente}: {total} ítems guardados; se muestran {len(items)} (los últimos insertados)\n")
    for n, i in enumerate(items, 1):
        extracto = i.texto[: args.texto] + ("…" if len(i.texto) > args.texto else "")
        print(f"[{n}] {i.titulo or '(sin título)'}")
        print(
            f"    tipo: {i.tipo_fuente} · método: {i.metodo_extraccion} · "
            f"calidad: {i.calidad_extraccion} · estado al guardar: {i.estado}"
        )
        print(f"    publicado: {i.fecha_publicacion or 'sin fecha'} · capturado: {i.fecha_captura}")
        print(f"    url: {i.url_canonica}")
        print(f"    texto: {extracto}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

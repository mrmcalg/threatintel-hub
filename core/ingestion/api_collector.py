# core/ingestion/api_collector.py
"""Colector del catálogo CISA KEV (documento v2, secciones 4.1 y 4.4).

Es el primer colector de tipo `api`. El documento lo describe como colector
"genérico de APIs JSON", pero con una sola API no hay base para generalizar:
cuando exista una segunda API se extraerá lo común.

Formato asumido del JSON (de memoria, hay que contrastarlo con el archivo real
descargado en tests/fixtures/): un objeto con `count` y `vulnerabilities`, y
cada entrada con `cveID`, `vendorProject`, `product`, `vulnerabilityName`,
`dateAdded`, `shortDescription`, `requiredAction`, `dueDate`,
`knownRansomwareCampaignUse` y `notes`.

Decisiones mías que el documento no precisa:
- Una entrada KEV no trae URL propia: `url_original` apunta a la ficha del CVE
  en NVD (plantilla en config/settings.py).
- `fecha_publicacion` es `dateAdded` (fecha en que CISA añadió el CVE), a las
  00:00 UTC porque el catálogo solo da la fecha.
- Una entrada con `cveID` inválido se omite y se cuenta en `omitidos`.
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from config import settings
from core.ingestion.base import (
    BaseCollector,
    ErrorColector,
    ResultadoRecoleccion,
    Validadores,
)
from core.ingestion.normalizer import (
    canonical_url,
    content_hash,
    make_item_id,
    normalize_text,
)
from core.models.raw_item import RawItem

_RE_CVE = re.compile(r"^CVE-\d{4}-\d{4,7}$")


class RespuestaApiInvalida(ErrorColector):
    """La respuesta no tiene la estructura esperada del catálogo."""


def _txt(valor) -> str:
    return str(valor).strip() if valor is not None else ""


def _fecha(valor) -> Optional[datetime]:
    try:
        return datetime.strptime(_txt(valor), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _texto_kev(cve: str, v: dict) -> str:
    campos = [
        (None, f"{cve}: {_txt(v.get('vulnerabilityName'))}"),
        ("Proveedor", _txt(v.get("vendorProject"))),
        ("Producto", _txt(v.get("product"))),
        ("Descripción", _txt(v.get("shortDescription"))),
        ("Acción requerida", _txt(v.get("requiredAction"))),
        ("Fecha límite de remediación", _txt(v.get("dueDate"))),
        ("Uso en campañas de ransomware", _txt(v.get("knownRansomwareCampaignUse"))),
        ("Notas", _txt(v.get("notes"))),
    ]
    lineas = []
    for etiqueta, valor in campos:
        if not valor:
            continue
        lineas.append(valor if etiqueta is None else f"{etiqueta}: {valor}.")
    return normalize_text(" ".join(lineas))


class KevCollector(BaseCollector):
    tipo = "api"

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

        try:
            datos = json.loads(resp.contenido)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RespuestaApiInvalida(
                f"La respuesta de '{self._fuente.id}' no es JSON válido: {e}"
            ) from e
        if not isinstance(datos, dict) or not isinstance(datos.get("vulnerabilities"), list):
            raise RespuestaApiInvalida(
                f"'{self._fuente.id}' no trae la lista 'vulnerabilities' esperada"
            )

        entradas = datos["vulnerabilities"]
        advertencias: list[str] = []
        declarado = datos.get("count")
        if isinstance(declarado, int) and declarado != len(entradas):
            advertencias.append(
                f"El catálogo declara count={declarado} pero trae {len(entradas)} entradas"
            )

        ahora = self._ahora()
        items: list[RawItem] = []
        omitidos = 0
        for entrada in entradas:
            item = self._a_item(entrada, ahora) if isinstance(entrada, dict) else None
            if item is None:
                omitidos += 1
            else:
                items.append(item)
        if omitidos:
            advertencias.append(f"{omitidos} entradas omitidas por cveID inválido")

        return ResultadoRecoleccion(
            fuente_id=self._fuente.id,
            status=resp.status,
            items=items,
            etag=resp.etag,
            last_modified=resp.last_modified,
            omitidos=omitidos,
            advertencias=advertencias,
        )

    def _a_item(self, v: dict, ahora: datetime) -> Optional[RawItem]:
        cve = _txt(v.get("cveID")).upper()
        if not _RE_CVE.match(cve):
            return None

        url = settings.URL_DETALLE_CVE.format(cve=cve)
        url_canonica = canonical_url(url)
        texto = _texto_kev(cve, v)

        return RawItem(
            item_id=make_item_id(self._fuente.id, url_canonica),
            fuente_id=self._fuente.id,
            tipo_fuente="api",
            url_original=url,
            url_canonica=url_canonica,
            guid=cve,
            titulo=_txt(v.get("vulnerabilityName")) or cve,
            autor=None,
            fecha_publicacion=_fecha(v.get("dateAdded")),
            fecha_captura=ahora,
            idioma="en",
            texto=texto,
            hash_contenido=content_hash(texto),
            metodo_extraccion="api_json",
            calidad_extraccion="completa",
            estado="nuevo",
        )

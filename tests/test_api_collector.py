# tests/test_api_collector.py
# El JSON sintético de este archivo está escrito a mano (el CVE-2026-35273 es
# el ejemplo ilustrativo del documento, no un CVE real). Las pruebas con el
# catálogo real se activan si existe tests/fixtures/known_exploited_vulnerabilities.json.
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.ingestion.api_collector import KevCollector, RespuestaApiInvalida
from core.ingestion.base import Validadores
from core.ingestion.dedup import ItemStore
from core.ingestion.http_client import Respuesta
from core.models.source_config import Fuente

pytestmark = pytest.mark.anyio

URL = "https://ejemplo.com/kev.json"
AHORA = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)
REAL = Path(__file__).resolve().parent / "fixtures" / "known_exploited_vulnerabilities.json"

VULN_A = {
    "cveID": "CVE-2026-35273",
    "vendorProject": "Enterprise Vendor",
    "product": "Gateway OS",
    "vulnerabilityName": "Enterprise Vendor Gateway OS RCE Vulnerability",
    "dateAdded": "2026-10-08",
    "shortDescription": "Gateway OS contains an unauthenticated remote code execution vulnerability.",
    "requiredAction": "Apply mitigations per vendor instructions.",
    "dueDate": "2026-10-29",
    "knownRansomwareCampaignUse": "Unknown",
    "notes": "",
    "cwes": ["CWE-78"],
}
VULN_B = {
    "cveID": "CVE-2026-10001",
    "vendorProject": "Otro Proveedor",
    "product": "Panel",
    "vulnerabilityName": "Otro Proveedor Panel Auth Bypass",
    "dateAdded": "2026-10-07",
    "shortDescription": "Panel permite omitir la autenticación.",
    "requiredAction": "Aplicar el parche del fabricante.",
    "dueDate": "2026-10-28",
    "knownRansomwareCampaignUse": "Known",
    "notes": "Ver aviso del fabricante",
    "cwes": [],
}
VULN_MALA = {**VULN_A, "cveID": "no-es-un-cve"}


def kev_json(vulns, count=None) -> bytes:
    cuerpo = {
        "title": "CISA Catalog of Known Exploited Vulnerabilities",
        "catalogVersion": "2026.10.08",
        "dateReleased": "2026-10-08T14:00:00.000Z",
        "count": len(vulns) if count is None else count,
        "vulnerabilities": vulns,
    }
    return json.dumps(cuerpo).encode("utf-8")


class FalsoCliente:
    def __init__(self, respuesta: Respuesta):
        self.respuesta = respuesta
        self.llamadas: list[tuple] = []

    async def obtener(self, url, *, etag=None, last_modified=None):
        self.llamadas.append((url, etag, last_modified))
        return self.respuesta


def hacer_fuente(tipo="api", fuente_id="cisa_kev_api"):
    return Fuente(
        id=fuente_id,
        nombre="CISA KEV",
        tipo=tipo,
        url=URL,
        activo=True,
        categoria="oficial_gobierno",
        frecuencia_minutos=60,
        fiabilidad="alta",
    )


def hacer_colector(cuerpo: bytes, *, status=200):
    cliente = FalsoCliente(Respuesta(status=status, contenido=cuerpo, url_final=URL))
    return KevCollector(hacer_fuente(), cliente, ahora=lambda: AHORA), cliente


# ------------------------------------------------------------------ mapeo
async def test_kev_mapea_el_contrato_del_item():
    colector, _ = hacer_colector(kev_json([VULN_A, VULN_B]))
    r = await colector.recolectar()
    assert r.status == 200
    assert len(r.items) == 2
    assert r.omitidos == 0
    assert r.advertencias == []

    i = r.items[0]
    assert i.fuente_id == "cisa_kev_api"
    assert i.tipo_fuente == "api"
    assert i.guid == "CVE-2026-35273"
    assert i.titulo == "Enterprise Vendor Gateway OS RCE Vulnerability"
    assert i.url_canonica == "https://nvd.nist.gov/vuln/detail/CVE-2026-35273"
    assert i.fecha_publicacion == datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert i.fecha_captura == AHORA
    assert i.idioma == "en"
    assert i.metodo_extraccion == "api_json"
    assert i.calidad_extraccion == "completa"
    assert i.estado == "nuevo"
    for fragmento in ("CVE-2026-35273", "Enterprise Vendor", "Gateway OS", "2026-10-29"):
        assert fragmento in i.texto


async def test_cve_en_minusculas_se_normaliza():
    minuscula = {**VULN_A, "cveID": "cve-2026-35273"}
    colector, _ = hacer_colector(kev_json([minuscula]))
    r = await colector.recolectar()
    assert r.items[0].guid == "CVE-2026-35273"


async def test_entrada_con_cve_invalido_se_omite():
    colector, _ = hacer_colector(kev_json([VULN_A, VULN_MALA]))
    r = await colector.recolectar()
    assert len(r.items) == 1
    assert r.omitidos == 1
    assert any("inválido" in a for a in r.advertencias)


async def test_count_distinto_genera_advertencia():
    colector, _ = hacer_colector(kev_json([VULN_A], count=5))
    r = await colector.recolectar()
    assert len(r.items) == 1
    assert any("count" in a for a in r.advertencias)


# ---------------------------------------------------------------- errores
async def test_json_invalido_falla():
    colector, _ = hacer_colector(b"<html>no es json</html>")
    with pytest.raises(RespuestaApiInvalida):
        await colector.recolectar()


async def test_json_sin_lista_de_vulnerabilidades_falla():
    colector, _ = hacer_colector(json.dumps({"title": "x"}).encode("utf-8"))
    with pytest.raises(RespuestaApiInvalida):
        await colector.recolectar()


def test_el_colector_rechaza_fuentes_de_otro_tipo():
    with pytest.raises(ValueError, match="solo acepta"):
        KevCollector(hacer_fuente(tipo="rss"), FalsoCliente(Respuesta(200, b"", URL)))


# ----------------------------------------------------- condicionales / dedup
async def test_304_conserva_los_validadores_previos():
    colector, cliente = hacer_colector(b"", status=304)
    r = await colector.recolectar(Validadores(etag='"k1"', last_modified="X"))
    assert r.no_modificado is True
    assert r.items == []
    assert r.etag == '"k1"'
    assert cliente.llamadas == [(URL, '"k1"', "X")]


async def test_reingesta_del_catalogo_no_crea_duplicados():
    colector, _ = hacer_colector(kev_json([VULN_A, VULN_B]))
    store = ItemStore(":memory:")
    try:
        primera = [store.registrar(i).estado for i in (await colector.recolectar()).items]
        segunda = [store.registrar(i).estado for i in (await colector.recolectar()).items]
        assert primera == ["nuevo", "nuevo"]
        assert segunda == ["duplicado", "duplicado"]
        assert store.contar_items() == 2
    finally:
        store.cerrar()


async def test_cambio_en_una_entrada_se_marca_actualizado():
    colector_1, _ = hacer_colector(kev_json([VULN_A]))
    colector_2, _ = hacer_colector(kev_json([{**VULN_A, "dueDate": "2026-11-15"}]))
    store = ItemStore(":memory:")
    try:
        original = store.registrar((await colector_1.recolectar()).items[0])
        cambiado = store.registrar((await colector_2.recolectar()).items[0])
        assert original.estado == "nuevo"
        assert cambiado.estado == "actualizado"
        assert cambiado.item_id == original.item_id
        assert store.contar_items() == 1
    finally:
        store.cerrar()


# --------------------------------------------------------- catálogo real
solo_si_hay_catalogo_real = pytest.mark.skipif(
    not REAL.exists(),
    reason="Falta tests/fixtures/known_exploited_vulnerabilities.json",
)


@solo_si_hay_catalogo_real
async def test_catalogo_real_se_procesa():
    colector, _ = hacer_colector(REAL.read_bytes())
    r = await colector.recolectar()

    ransomware = Counter(
        v.get("knownRansomwareCampaignUse") for v in json.loads(REAL.read_bytes())["vulnerabilities"]
    )
    print(
        f"\n[KEV real] items={len(r.items)} omitidos={r.omitidos} "
        f"advertencias={r.advertencias} ransomware={dict(ransomware)}"
    )

    assert len(r.items) > 0
    assert r.omitidos == 0
    assert all(re.match(r"^CVE-\d{4}-\d{4,7}$", i.guid) for i in r.items)
    ids = [i.item_id for i in r.items]
    assert len(ids) == len(set(ids)), "item_id repetido en el catálogo"


@solo_si_hay_catalogo_real
async def test_catalogo_real_es_idempotente():
    colector, _ = hacer_colector(REAL.read_bytes())
    store = ItemStore(":memory:")
    try:
        for item in (await colector.recolectar()).items:
            store.registrar(item)
        cuenta = store.contar_items()
        segunda = [store.registrar(i).estado for i in (await colector.recolectar()).items]
        assert set(segunda) == {"duplicado"}
        assert store.contar_items() == cuenta
    finally:
        store.cerrar()

# tests/test_http_client.py
import httpx
import pytest

from config import settings
from core.ingestion.http_client import (
    DemasiadasRedirecciones,
    ErrorDeRed,
    ErrorHttp,
    HttpClient,
    RespuestaDemasiadoGrande,
    UrlNoPermitida,
    _ip_no_publica,
)
from core.models.source_config import Defaults

pytestmark = pytest.mark.anyio

URL = "https://ejemplo.com/feed"

TABLA_DNS = {
    "ejemplo.com": ["8.8.8.8"],
    "otro.ejemplo.com": ["8.8.4.4"],
    "interno.ejemplo.com": ["10.0.0.5"],
    "metadatos.ejemplo.com": ["169.254.169.254"],
}


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def resolver_falso(host):
    return TABLA_DNS.get(host, ["8.8.8.8"])


def hacer_cliente(**kw):
    esperas: list[float] = []

    async def dormir(segundos):
        esperas.append(segundos)

    params = dict(
        timeout_segundos=5,
        max_bytes=1000,
        reintentos=2,
        user_agent="Test/1.0",
        resolver=resolver_falso,
        dormir=dormir,
    )
    params.update(kw)
    return HttpClient(**params), esperas


# --------------------------------------------------------------- respuestas
async def test_200_devuelve_contenido_y_validadores(respx_mock):
    respx_mock.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b"hola",
            headers={"ETag": '"abc"', "Last-Modified": "Wed, 07 Oct 2026 10:00:00 GMT"},
        )
    )
    cliente, _ = hacer_cliente()
    r = await cliente.obtener(URL)
    assert r.status == 200
    assert r.contenido == b"hola"
    assert r.etag == '"abc"'
    assert r.last_modified == "Wed, 07 Oct 2026 10:00:00 GMT"
    assert r.no_modificado is False


async def test_envia_condicionales_y_304_es_exito_sin_contenido(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(304))
    cliente, _ = hacer_cliente()
    r = await cliente.obtener(
        URL, etag='"abc"', last_modified="Wed, 07 Oct 2026 10:00:00 GMT"
    )
    assert r.no_modificado is True
    assert r.contenido == b""
    peticion = ruta.calls.last.request
    assert peticion.headers["if-none-match"] == '"abc"'
    assert peticion.headers["if-modified-since"] == "Wed, 07 Oct 2026 10:00:00 GMT"


async def test_envia_user_agent_configurado(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"ok"))
    cliente, _ = hacer_cliente()
    await cliente.obtener(URL)
    assert ruta.calls.last.request.headers["user-agent"] == "Test/1.0"


async def test_desde_defaults_usa_el_user_agent_del_yaml(respx_mock):
    defaults = Defaults(
        timeout_segundos=5,
        max_bytes=1000,
        reintentos=0,
        user_agent="UA/9",
        respetar_robots_txt=True,
    )
    cliente = HttpClient.desde_defaults(defaults, resolver=resolver_falso)
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"ok"))
    await cliente.obtener(URL)
    assert ruta.calls.last.request.headers["user-agent"] == "UA/9"


# ---------------------------------------------------------------- reintentos
async def test_429_respeta_retry_after_y_reintenta(respx_mock):
    ruta = respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    cliente, esperas = hacer_cliente()
    r = await cliente.obtener(URL)
    assert r.contenido == b"ok"
    assert esperas == [3.0]
    assert ruta.call_count == 2


async def test_503_agota_reintentos(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    cliente, esperas = hacer_cliente()  # reintentos=2 -> 3 intentos
    with pytest.raises(ErrorHttp) as exc:
        await cliente.obtener(URL)
    assert exc.value.status == 503
    assert ruta.call_count == 3
    assert len(esperas) == 2
    assert all(0 <= e <= settings.BACKOFF_MAX_SEGUNDOS for e in esperas)


async def test_404_no_reintenta(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(404))
    cliente, esperas = hacer_cliente()
    with pytest.raises(ErrorHttp) as exc:
        await cliente.obtener(URL)
    assert exc.value.status == 404
    assert ruta.call_count == 1
    assert esperas == []


async def test_error_de_transporte_reintenta_y_luego_falla(respx_mock):
    ruta = respx_mock.get(URL).mock(side_effect=httpx.ConnectError("sin conexion"))
    cliente, _ = hacer_cliente()
    with pytest.raises(ErrorDeRed):
        await cliente.obtener(URL)
    assert ruta.call_count == 3


async def test_retry_after_excesivo_no_insiste(respx_mock):
    ruta = respx_mock.get(URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    cliente, esperas = hacer_cliente()
    with pytest.raises(ErrorHttp) as exc:
        await cliente.obtener(URL)
    assert exc.value.retry_after == 3600.0
    assert ruta.call_count == 1
    assert esperas == []


# ------------------------------------------------------------------ tamaño
async def test_content_length_mayor_al_maximo_falla(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"x" * 2000))
    cliente, _ = hacer_cliente()
    with pytest.raises(RespuestaDemasiadoGrande):
        await cliente.obtener(URL)


async def test_respuesta_en_trozos_sin_content_length_mayor_al_maximo_falla(respx_mock):
    async def trozos():
        yield b"x" * 600
        yield b"x" * 600

    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=trozos()))
    cliente, _ = hacer_cliente()
    with pytest.raises(RespuestaDemasiadoGrande):
        await cliente.obtener(URL)


# ----------------------------------------------------------------- anti-SSRF
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://ejemplo.com/x"])
async def test_rechaza_esquemas_no_http(url, respx_mock):
    cliente, _ = hacer_cliente()
    with pytest.raises(UrlNoPermitida):
        await cliente.obtener(url)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "[::1]", "100.64.0.1"],
)
async def test_rechaza_ip_literal_no_publica(host, respx_mock):
    cliente, _ = hacer_cliente()
    with pytest.raises(UrlNoPermitida):
        await cliente.obtener(f"http://{host}/")


async def test_rechaza_hostname_que_resuelve_a_ip_interna(respx_mock):
    cliente, _ = hacer_cliente()
    with pytest.raises(UrlNoPermitida):
        await cliente.obtener("https://interno.ejemplo.com/feed")


async def test_bloquea_redireccion_a_destino_interno(respx_mock):
    ruta = respx_mock.get(URL).mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://metadatos.ejemplo.com/latest/"}
        )
    )
    cliente, _ = hacer_cliente()
    with pytest.raises(UrlNoPermitida):
        await cliente.obtener(URL)
    assert ruta.call_count == 1


async def test_sigue_redireccion_a_destino_publico(respx_mock):
    respx_mock.get(URL).mock(
        return_value=httpx.Response(301, headers={"Location": "https://otro.ejemplo.com/b"})
    )
    respx_mock.get("https://otro.ejemplo.com/b").mock(
        return_value=httpx.Response(200, content=b"fin")
    )
    cliente, _ = hacer_cliente()
    r = await cliente.obtener(URL)
    assert r.url_final == "https://otro.ejemplo.com/b"
    assert r.contenido == b"fin"


async def test_demasiadas_redirecciones_falla(respx_mock):
    ruta = respx_mock.get(URL).mock(
        return_value=httpx.Response(302, headers={"Location": URL})
    )
    cliente, _ = hacer_cliente(max_redirecciones=2)
    with pytest.raises(DemasiadasRedirecciones):
        await cliente.obtener(URL)
    assert ruta.call_count == 3


@pytest.mark.parametrize(
    "ip, esperado",
    [("8.8.8.8", False), ("::ffff:127.0.0.1", True), ("198.51.100.7", True)],
)
def test_clasificacion_de_ips(ip, esperado):
    assert _ip_no_publica(ip) is esperado

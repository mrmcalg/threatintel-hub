from datetime import datetime, timezone, timedelta
from core.ingestion.normalizer import canonical_url, to_utc, content_hash


def test_quita_tracking_y_fragmento():
    assert canonical_url("HTTPS://Ejemplo.com/a?utm_source=x&id=1#top") == "https://ejemplo.com/a?id=1"


def test_quita_parametros_de_settings():
    assert canonical_url("https://ejemplo.com/a?fbclid=abc&id=1") == "https://ejemplo.com/a?id=1"


def test_fecha_a_utc():
    d = datetime(2026, 10, 6, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert to_utc(d).hour == 14


def test_fecha_none_no_se_inventa():
    assert to_utc(None) is None


def test_hash_ignora_espacios():
    assert content_hash("hola  mundo\n") == content_hash("hola mundo")
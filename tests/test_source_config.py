# tests/test_source_config.py
from pathlib import Path

import pytest

from core.models.source_config import ConfigError, load_sources

REAL_YAML = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

HEAD = """version: 2
defaults:
  timeout_segundos: 20
  max_bytes: 5242880
  reintentos: 3
  user_agent: "Test/1.0"
  respetar_robots_txt: true
fuentes:
"""

FUENTE = """  - id: "feed_a"
    nombre: "Feed A"
    tipo: "rss"
    url: "https://ejemplo.com/feed"
    activo: true
    categoria: "noticias_soc"
    frecuencia_minutos: 30
    fiabilidad: "media"
"""

WEB = """    web:
      modo: "listado"
      patron_enlaces: "/a/[a-z]+"
      render_js: false
"""


def escribir(tmp_path, texto: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(texto, encoding="utf-8")
    return p


def test_yaml_real_del_proyecto_es_valido():
    cfg = load_sources(REAL_YAML)
    assert len(cfg.fuentes) >= 1


def test_configuracion_minima_valida(tmp_path):
    cfg = load_sources(escribir(tmp_path, HEAD + FUENTE))
    assert cfg.fuentes[0].id == "feed_a"
    assert cfg.fuentes[0].rol == ["fuente"]


def test_id_duplicado_falla(tmp_path):
    with pytest.raises(ConfigError, match="duplicado"):
        load_sources(escribir(tmp_path, HEAD + FUENTE + FUENTE))


def test_tipo_web_sin_bloque_web_falla(tmp_path):
    texto = HEAD + FUENTE.replace('tipo: "rss"', 'tipo: "web"')
    with pytest.raises(ConfigError, match="requiere el bloque"):
        load_sources(escribir(tmp_path, texto))


def test_tipo_web_con_bloque_valido(tmp_path):
    texto = HEAD + FUENTE.replace('tipo: "rss"', 'tipo: "web"') + WEB
    cfg = load_sources(escribir(tmp_path, texto))
    assert cfg.fuentes[0].web.render_js is False


def test_regex_invalida_falla(tmp_path):
    texto = HEAD + FUENTE.replace('tipo: "rss"', 'tipo: "web"') + WEB.replace("/a/[a-z]+", "[")
    with pytest.raises(ConfigError, match="regex"):
        load_sources(escribir(tmp_path, texto))


def test_tipo_invalido_falla(tmp_path):
    texto = HEAD + FUENTE.replace('tipo: "rss"', 'tipo: "ftp"')
    with pytest.raises(ConfigError, match="fuentes.0.tipo"):
        load_sources(escribir(tmp_path, texto))


def test_url_con_esquema_no_http_falla(tmp_path):
    texto = HEAD + FUENTE.replace("https://ejemplo.com/feed", "ftp://ejemplo.com/feed")
    with pytest.raises(ConfigError, match="fuentes.0.url"):
        load_sources(escribir(tmp_path, texto))


def test_campo_desconocido_falla(tmp_path):
    texto = HEAD + FUENTE + "    frecuencia_minuto: 5\n"
    with pytest.raises(ConfigError, match="frecuencia_minuto"):
        load_sources(escribir(tmp_path, texto))


def test_frecuencia_cero_falla(tmp_path):
    texto = HEAD + FUENTE.replace("frecuencia_minutos: 30", "frecuencia_minutos: 0")
    with pytest.raises(ConfigError, match="frecuencia_minutos"):
        load_sources(escribir(tmp_path, texto))


def test_archivo_inexistente_falla(tmp_path):
    with pytest.raises(ConfigError, match="No existe"):
        load_sources(tmp_path / "no_existe.yaml")

# core/models/source_config.py
"""Validación de config/sources.yaml (documento v2, secciones 4.3 y 4.10).

Principios:
- Los campos desconocidos se rechazan (extra="forbid") para detectar errores
  de tipeo en el YAML.
- Los valores de `defaults` son obligatorios en el YAML: el código no
  inventa valores por su cuenta.
"""
import re
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(ValueError):
    """Error de configuración con mensaje legible para quien edita el YAML."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Defaults(_Base):
    timeout_segundos: int = Field(gt=0)
    max_bytes: int = Field(gt=0)
    reintentos: int = Field(ge=0)
    user_agent: str = Field(min_length=1)
    respetar_robots_txt: bool


class WebConfig(_Base):
    modo: Literal["listado"]
    patron_enlaces: str = Field(min_length=1)
    render_js: bool = False

    @field_validator("patron_enlaces")
    @classmethod
    def _regex_valida(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"regex inválida en patron_enlaces: {e}")
        return v


class Fuente(_Base):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    nombre: str = Field(min_length=1)
    tipo: Literal["rss", "api", "web"]
    url: HttpUrl
    activo: bool
    categoria: str = Field(min_length=1)
    frecuencia_minutos: int = Field(gt=0)
    fiabilidad: Literal["alta", "media", "baja"]
    # Si no se declara, la fuente solo produce ítems (suposición mía).
    rol: list[Literal["fuente", "enriquecedor"]] = Field(default_factory=lambda: ["fuente"])
    web: Optional[WebConfig] = None

    @model_validator(mode="after")
    def _coherencia_web(self):
        if self.tipo == "web" and self.web is None:
            raise ValueError("tipo 'web' requiere el bloque 'web'")
        if self.tipo != "web" and self.web is not None:
            raise ValueError("el bloque 'web' solo aplica a tipo 'web'")
        return self


class Enriquecedor(_Base):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    nombre: str = Field(min_length=1)
    url_api: HttpUrl
    modo: Optional[str] = None
    cache_horas: int = Field(gt=0)
    requiere_api_key: bool = False
    activo: bool


class SourcesFile(_Base):
    version: Literal[2]
    defaults: Defaults
    fuentes: list[Fuente] = Field(min_length=1)
    enriquecedores: list[Enriquecedor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_unicos(self):
        for nombre, items in (("fuentes", self.fuentes), ("enriquecedores", self.enriquecedores)):
            vistos: set[str] = set()
            for item in items:
                if item.id in vistos:
                    raise ValueError(f"id duplicado en {nombre}: '{item.id}'")
                vistos.add(item.id)
        return self


def _formatear(path: Path, exc: ValidationError) -> str:
    lineas = [f"Configuración inválida en {path}:"]
    for err in exc.errors():
        ubicacion = ".".join(str(x) for x in err["loc"]) or "(raíz)"
        lineas.append(f"  - {ubicacion}: {err['msg']}")
    return "\n".join(lineas)


def load_sources(path: Union[str, Path]) -> SourcesFile:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"No existe el archivo de fuentes: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML mal formado en {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p} debe contener un mapa YAML en la raíz")
    try:
        return SourcesFile.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_formatear(p, e)) from e

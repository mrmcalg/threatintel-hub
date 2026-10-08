from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel


class RawItem(BaseModel):
    item_id: str
    fuente_id: str
    tipo_fuente: Literal["rss", "api", "web"]
    url_original: str
    url_canonica: str
    guid: Optional[str] = None
    titulo: str
    autor: Optional[str] = None
    fecha_publicacion: Optional[datetime] = None  # UTC; None si la fuente no la trae
    fecha_captura: datetime
    idioma: Optional[str] = None
    texto: str
    hash_contenido: str
    metodo_extraccion: Literal[
        "feed_summary", "feed_content", "trafilatura", "playwright+trafilatura"
    ]
    calidad_extraccion: Literal["completa", "parcial", "solo_resumen"]
    estado: Literal["nuevo", "actualizado", "duplicado"]
    raw_ref: Optional[str] = None
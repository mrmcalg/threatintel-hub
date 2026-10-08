import hashlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    netloc = host
    default = (scheme == "http" and p.port == 80) or (scheme == "https" and p.port == 443)
    if p.port and not default:
        netloc = f"{host}:{p.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, p.path or "/", urlencode(query), ""))


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Suposición mía: una fecha sin zona horaria se trata como UTC.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def make_item_id(fuente_id: str, url_canonica: str) -> str:
    return hashlib.sha256(f"{fuente_id}{url_canonica}".encode("utf-8")).hexdigest()
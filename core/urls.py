"""Normalización de URLs para deduplicación exacta e historial `sent_urls`."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "mc_", "_hs", "hsa_", "vero_", "mkt_")
_TRACKING_KEYS = {"fbclid", "gclid", "yclid", "igshid", "ref", "ref_src", "cmpid", "s_cid", "amp"}


def normalize_url(url: str) -> str:
    """Quita tracking, fragmentos, `www.` y barra final; fuerza https."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def domain_of(url: str) -> str:
    host = urlsplit(url if "://" in url else f"https://{url}").netloc.lower()
    return host[4:] if host.startswith("www.") else host

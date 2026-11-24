"""Cliente HTTP compartido con timeouts, User-Agent y reintentos acotados.

Reintenta solo errores transitorios (red, 429, 5xx). Un 403/404 se propaga de
inmediato: suele indicar bloqueo anti-bot o link roto, y lo registra el
orquestador sin insistir (nunca intentamos evadir bloqueos ni paywalls).
"""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; GT-IntelBot/1.0; +https://gt.com.pe) "
    "Newsletter interno Global Transport"
)


def build_client(timeout: float = 20.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-PE,es;q=0.9,en;q=0.7",
        },
        transport=transport,
    )


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError,))


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    response = client.get(url, **kwargs)
    response.raise_for_status()
    return response

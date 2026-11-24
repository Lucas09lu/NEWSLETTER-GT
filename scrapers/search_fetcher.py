"""Conector de búsqueda indexada (`site:dominio`, últimos 7 días) vía Brave Search API.

Es el fallback obligatorio del PRD §4.3 para fuentes que bloquean la lectura
automática, no exponen RSS o tienen paywall. Nunca visita la página del
artículo: usa solo el título, la URL y el snippet público que devuelve el
buscador, lo que cumple la regla anti-paywall por diseño.

Docs: https://api-dashboard.search.brave.com/app/documentation/web-search
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import timezone

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from core.http import get
from core.models import ConnectorType, Source
from core.timewindow import LIMA, in_window
from core.urls import domain_of
from scrapers.base import Connector, ConnectorContext, ConnectorOutput, SkipConnector

log = logging.getLogger(__name__)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_SITE_RE = re.compile(r"site:(\S+)")
_GOBPE_INSTITUTION = re.compile(r"/institucion/([\w-]+)")


class _RateLimiter:
    """Garantiza un intervalo mínimo entre consultas (plan gratuito = 1 qps)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, min_interval: float) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < min_interval:
                time.sleep(min_interval - delta)
            self._last = time.monotonic()


_limiter = _RateLimiter()


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True) or None


def freshness_param(ctx: ConnectorContext) -> str:
    start = ctx.window.start.astimezone(LIMA).date()
    end = ctx.window.end.astimezone(LIMA).date()
    return f"{start.isoformat()}to{end.isoformat()}"


def result_matches_source(url: str, source: Source, site: str | None) -> bool:
    """Descarta resultados fuera del dominio (o de otra institución en gob.pe)."""
    host = domain_of(url)
    expected = domain_of(site.split("/")[0]) if site else source.domain
    if not (host == expected or host.endswith("." + expected)):
        return False
    institution = _GOBPE_INSTITUTION.search(source.link) if source.domain == "gob.pe" else None
    if institution and f"/institucion/{institution.group(1)}/" not in url:
        return False
    return True


class BraveSearchConnector(Connector):
    type = ConnectorType.SEARCH

    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput:
        if not ctx.settings.has_brave:
            raise SkipConnector("BRAVE_API_KEY no configurada")
        query = source.search_query or f"site:{source.domain}"
        site_match = _SITE_RE.search(query)
        site = site_match.group(1) if site_match else None

        _limiter.wait(ctx.settings.brave_min_interval_seconds)
        response = get(
            ctx.client,
            BRAVE_ENDPOINT,
            params={"q": query, "count": 20, "freshness": freshness_param(ctx), "safesearch": "off"},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": ctx.settings.brave_api_key.get_secret_value(),
            },
        )
        results = (response.json().get("web") or {}).get("results") or []

        items = []
        for result in results:
            url, title = result.get("url"), _clean(result.get("title"))
            if not url or not title or not result_matches_source(url, source, site):
                continue
            published = None
            if result.get("page_age"):
                try:
                    published = dateparser.parse(result["page_age"])
                    published = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
                except (ValueError, OverflowError):
                    published = None
            # Brave ya filtró por `freshness`; si informa fecha y cae fuera, se descarta.
            if published is not None and not in_window(published, ctx.window):
                continue
            items.append(
                self.make_item(
                    source,
                    ctx,
                    title=title,
                    url=url,
                    published_at=published,
                    summary=_clean(result.get("description")),
                    extra={"date_from": "page_age" if published else "search_freshness", "query": query},
                )
            )
        return ConnectorOutput(items=items, message=f"q={query!r}: {len(results)} resultados")

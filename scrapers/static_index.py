"""Monitor de páginas índice de informes periódicos (IATA, OMC, UNCTAD, BID INTAL).

En lugar de leer siempre el mismo informe histórico (el link del Excel apunta
a una edición concreta), se vigila la página índice del organismo y se emiten
solo los enlaces que coinciden con `index_link_pattern` y que no se habían visto
antes. El registro `static_index_seen.json` guarda la fecha de primera
detección de cada enlace.

Primera ejecución = línea base: se registran los enlaces existentes sin
emitirlos, para no publicar informes antiguos.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.http import get
from core.models import ConnectorType, Source
from core.urls import normalize_url
from scrapers.base import Connector, ConnectorContext, ConnectorOutput, SkipConnector

log = logging.getLogger(__name__)

STATE_KEY = "static_index_seen"
BASELINE = "baseline"


def extract_index_links(html: str, base_url: str, pattern: str) -> list[tuple[str, str]]:
    regex = re.compile(pattern)
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"].strip())
        if not regex.search(url):
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())
        key = normalize_url(url)
        # Conserva el texto de ancla más descriptivo para el mismo destino
        if len(text) > len(found.get(key, ("", ""))[1]):
            found[key] = (url, text)
    return list(found.values())


class StaticIndexConnector(Connector):
    type = ConnectorType.STATIC_INDEX

    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput:
        if not source.index_url or not source.index_link_pattern:
            raise SkipConnector("Sin index_url / index_link_pattern configurados")
        html = get(ctx.client, source.index_url).text
        links = extract_index_links(html, source.index_url, source.index_link_pattern)

        state: dict = ctx.state.load(STATE_KEY, {})
        seen: dict[str, str] = state.setdefault(str(source.id), {})
        is_baseline = not seen
        now_iso = ctx.now.isoformat()

        items = []
        for url, text in links:
            key = normalize_url(url)
            first_seen = seen.get(key)
            if first_seen is None:
                seen[key] = BASELINE if is_baseline else now_iso
                first_seen = seen[key]
            # Se emite si apareció por primera vez durante esta semana editorial
            # (incluye re-ejecuciones del mismo lunes).
            if first_seen != BASELINE and datetime.fromisoformat(first_seen) >= ctx.window.start:
                items.append(
                    self.make_item(
                        source,
                        ctx,
                        title=text or url,
                        url=url,
                        published_at=None,
                        extra={"date_from": "first_seen", "first_seen": first_seen, "index_url": source.index_url},
                    )
                )
        ctx.state.save(STATE_KEY, state)
        msg = f"{len(links)} enlaces en índice" + (" (línea base registrada)" if is_baseline else "")
        return ConnectorOutput(items=items, message=msg)

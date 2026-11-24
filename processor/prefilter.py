"""Filtro determinista previo a la IA (barato y auditable).

1. Ventana estricta lunes→domingo hora Lima. Excepciones sin fecha propia:
   - `search`: Brave ya filtró por `freshness` con la ventana exacta;
   - `static_index`: se detectó como nuevo durante la semana.
2. Excluye URLs ya publicadas en ediciones anteriores (`sent_urls.json`).
"""

from __future__ import annotations

import logging
from datetime import date

from core.models import ConnectorType, IngestionResult, RawItem
from core.state import JsonStateStore
from core.timewindow import in_window
from core.urls import normalize_url

log = logging.getLogger(__name__)

SENT_KEY = "sent_urls"
_UNDATED_OK = {ConnectorType.SEARCH, ConnectorType.STATIC_INDEX}


def is_in_window(item: RawItem, result: IngestionResult) -> bool:
    if item.published_at is None:
        return item.connector in _UNDATED_OK
    return in_window(item.published_at, result.window)


def prefilter(result: IngestionResult, state: JsonStateStore) -> list[RawItem]:
    sent: dict = state.load(SENT_KEY, {})
    kept, out_of_window, already_sent = [], 0, 0
    for item in result.items:
        if not is_in_window(item, result):
            out_of_window += 1
            continue
        if normalize_url(item.url) in sent:
            already_sent += 1
            continue
        kept.append(item)
    log.info(
        "Prefiltro: %d ítems → %d (fuera de ventana: %d, ya publicados: %d)",
        len(result.items),
        len(kept),
        out_of_window,
        already_sent,
    )
    return kept


def mark_sent(state: JsonStateStore, urls: list[str], edition_date: date) -> None:
    """Registra las URLs publicadas para no repetirlas en semanas siguientes."""
    sent: dict = state.load(SENT_KEY, {})
    for url in urls:
        sent.setdefault(normalize_url(url), edition_date.isoformat())
    state.save(SENT_KEY, sent)

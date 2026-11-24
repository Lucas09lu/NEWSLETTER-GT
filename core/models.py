"""Modelos de datos compartidos entre módulos.

`RawItem` / `Indicator` / `IngestionResult` son el contrato de salida de la
capa de ingesta (`raw_items_YYYY-MM-DD.json`). La capa de curación (Fase 2)
solo depende de estos modelos, no de los conectores.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from core.urls import normalize_url


class ConnectorType(StrEnum):
    BCRP_API = "bcrp_api"
    RSS = "rss"
    INBOX = "inbox"
    SEARCH = "search"
    STATIC_INDEX = "static_index"


class Source(BaseModel):
    """Una fila de la hoja `Fuentes` + la estrategia de ingesta resuelta."""

    id: int
    group: str
    type: str
    content_type: str
    name: str
    description: str
    link: str
    note: str | None = None
    review_status: str
    how_to_get: str

    # --- Derivados / overrides ----------------------------------------------
    domain: str
    paywalled: bool = False
    connectors: list[ConnectorType] = Field(default_factory=list)
    # Conectores que solo se ejecutan si los previos no devolvieron ítems.
    fallback_only: list[ConnectorType] = Field(default_factory=list)
    feed_url: str | None = None
    search_query: str | None = None
    inbox_senders: list[str] = Field(default_factory=list)
    index_url: str | None = None
    index_link_pattern: str | None = None
    require_keywords: bool = False
    bcrp_series: list[str] = Field(default_factory=list)
    is_official: bool = False


class RawItem(BaseModel):
    """Una novedad candidata, normalizada, antes de la curación."""

    id: str = ""
    source_id: int
    source_name: str
    source_group: str
    connector: ConnectorType
    title: str
    url: str
    published_at: datetime | None = None
    summary: str | None = None
    paywalled: bool = False
    is_official: bool = False
    fetched_at: datetime
    extra: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = item_id(self.url)


class Indicator(BaseModel):
    """Una serie de cifras (BCRPData) para el Radar de Cifras."""

    series_code: str
    name: str
    period: str
    value: float | None
    previous_period: str | None = None
    previous_value: float | None = None
    source_name: str = "BCRPData"
    url: str


class SourceRunStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    SKIPPED = "skipped"


class SourceRun(BaseModel):
    """Resultado de ejecutar un conector sobre una fuente (log de salud)."""

    source_id: int
    source_name: str
    connector: ConnectorType | None = None  # None = fuente de consulta sin conector
    status: SourceRunStatus
    items: int = 0
    message: str | None = None
    duration_ms: int = 0


class TimeWindow(BaseModel):
    start: datetime
    end: datetime


class IngestionResult(BaseModel):
    edition_date: date
    window: TimeWindow
    generated_at: datetime
    sample: bool = False
    items: list[RawItem] = Field(default_factory=list)
    indicators: list[Indicator] = Field(default_factory=list)
    runs: list[SourceRun] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in SourceRunStatus}
        for run in self.runs:
            counts[run.status.value] += 1
        counts["items"] = len(self.items)
        counts["indicators"] = len(self.indicators)
        return counts


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]

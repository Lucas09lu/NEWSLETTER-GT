"""Contrato de `edition_YYYY-MM-DD.json`: fuente única para el correo y la PWA.

Diseñado para durar hasta V2 (suscripciones): todos los campos son explícitos
y versionados con `schema_version`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from core.models import TimeWindow

SCHEMA_VERSION = 1


class Pillar(StrEnum):
    LOGISTICA = "logistica"
    COMEX = "comex"
    REGULACION = "regulacion"
    SECTORIAL = "sectorial"
    GLOBAL = "global"


PILLAR_LABELS: dict[Pillar, str] = {
    Pillar.LOGISTICA: "Logística Global, Fletes y Puertos",
    Pillar.COMEX: "Comercio Exterior Peruano y Cifras Macro",
    Pillar.REGULACION: "Regulación, Aduanas y Acuerdos Comerciales",
    Pillar.SECTORIAL: "Pulso Sectorial: Agro, Minería e Industria",
    Pillar.GLOBAL: "Contexto Global y Tendencias",
}

PILLAR_ORDER = list(PILLAR_LABELS)


class SourceRef(BaseModel):
    name: str
    url: str


class Story(BaseModel):
    id: str
    pillar: Pillar
    headline: str
    what_happened: str
    impact: str
    source: SourceRef
    also_reported_by: list[SourceRef] = Field(default_factory=list)
    published_at: datetime | None = None
    is_official: bool = False
    paywalled: bool = False
    item_ids: list[str] = Field(default_factory=list)


class RadarEntry(BaseModel):
    label: str
    value: str
    period: str
    change: str | None = None
    trend: str | None = None  # "up" | "down" | "flat"
    source: SourceRef


class EditionStats(BaseModel):
    sources_total: int
    sources_ok: int
    sources_error: int
    items_raw: int
    items_considered: int


class Edition(BaseModel):
    schema_version: int = SCHEMA_VERSION
    edition_date: date
    window: TimeWindow
    generated_at: datetime
    curation_mode: str  # "claude" | "heuristic"
    sample: bool = False

    subject: str
    preheader: str
    main_theme: str
    executive_summary: list[str]
    stories: list[Story]
    radar: list[RadarEntry] = Field(default_factory=list)
    stats: EditionStats
    notes: list[str] = Field(default_factory=list)

    def stories_by_pillar(self) -> list[tuple[Pillar, str, list[Story]]]:
        out = []
        for pillar in PILLAR_ORDER:
            stories = [s for s in self.stories if s.pillar == pillar]
            if stories:
                out.append((pillar, PILLAR_LABELS[pillar], stories))
        return out

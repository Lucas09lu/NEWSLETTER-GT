"""Interfaz común de conectores de ingesta.

Cada conector implementa `fetch(source, ctx) -> ConnectorOutput` y puede:
- lanzar `SkipConnector` cuando falta configuración (p. ej. sin API key):
  el orquestador lo registra como "skipped", no como error;
- lanzar cualquier otra excepción: el orquestador la captura, la registra como
  "error" y continúa con el resto de fuentes (aislamiento por fuente).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from config.settings import Settings
from core.models import ConnectorType, Indicator, RawItem, Source, TimeWindow
from core.state import JsonStateStore


class SkipConnector(Exception):
    """El conector no puede ejecutarse por configuración (no es un fallo)."""


@dataclass
class ConnectorContext:
    settings: Settings
    client: httpx.Client
    window: TimeWindow
    edition_date: date
    state: JsonStateStore
    keywords: list[str] = field(default_factory=list)

    @property
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class ConnectorOutput:
    items: list[RawItem] = field(default_factory=list)
    indicators: list[Indicator] = field(default_factory=list)
    message: str | None = None


class Connector(ABC):
    type: ConnectorType

    @abstractmethod
    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput: ...

    # Ayudante común para construir RawItem coherentes
    def make_item(self, source: Source, ctx: ConnectorContext, **kwargs) -> RawItem:
        summary = kwargs.pop("summary", None)
        if summary:
            summary = " ".join(summary.split())
            # Regla anti-paywall: de fuentes con suscripción solo se guarda el
            # resumen público, recortado. Nunca se solicita el cuerpo.
            limit = 400 if source.paywalled else 1200
            summary = summary[:limit]
        return RawItem(
            source_id=source.id,
            source_name=source.name,
            source_group=source.group,
            connector=self.type,
            paywalled=source.paywalled,
            is_official=source.is_official,
            fetched_at=ctx.now,
            summary=summary,
            **kwargs,
        )

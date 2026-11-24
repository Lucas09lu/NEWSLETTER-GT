"""Radar de Cifras: se arma de forma determinista desde los indicadores (sin IA)."""

from __future__ import annotations

from core.edition import RadarEntry, SourceRef
from core.models import Indicator


def _fmt(value: float) -> str:
    text = f"{value:,.1f}" if abs(value) < 1000 else f"{value:,.0f}"
    # formato es-PE: miles con espacio fino no separable, decimal con punto
    return text.replace(",", " ")


def build_radar(indicators: list[Indicator]) -> list[RadarEntry]:
    entries: list[RadarEntry] = []
    for ind in indicators:
        if ind.value is None:
            continue
        change, trend = None, None
        if ind.previous_value not in (None, 0):
            pct = (ind.value - ind.previous_value) / abs(ind.previous_value) * 100
            trend = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
            change = f"{pct:+.1f}% vs {ind.previous_period}"
        entries.append(
            RadarEntry(
                label=ind.name,
                value=_fmt(ind.value),
                period=ind.period,
                change=change,
                trend=trend,
                source=SourceRef(name=ind.source_name, url=ind.url),
            )
        )
    return entries

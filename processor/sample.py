"""Semana de MUESTRA (datos ficticios) para previsualizar correo y app sin red.

Genera un `IngestionResult` con `sample=True` a partir de
`data/sample/sample_week.json`, con las fechas desplazadas a la ventana de la
edición. Todo lo que se genere a partir de él lleva el aviso de muestra.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from config.sources_loader import SourceCatalog
from core.models import ConnectorType, Indicator, IngestionResult, RawItem, SourceRun, SourceRunStatus
from core.timewindow import LIMA, edition_window

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "sample" / "sample_week.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def build_sample_result(catalog: SourceCatalog, edition_date: date, path: Path = SAMPLE_PATH) -> IngestionResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    window = edition_window(edition_date)
    monday = window.start.astimezone(LIMA).date()
    now = datetime.now(timezone.utc)

    items: list[RawItem] = []
    for row in data["items"]:
        source = catalog.by_id(row["source_id"])
        if source is None:
            continue
        published = datetime.combine(monday + timedelta(days=row["day"]), time(9, 0), tzinfo=LIMA)
        extra = {"sample": True}
        if row.get("draft"):
            extra["sample_draft"] = row["draft"]
        items.append(
            RawItem(
                source_id=source.id,
                source_name=source.name,
                source_group=source.group,
                connector=ConnectorType.RSS,
                title=row["title"],
                url=f"https://example.com/muestra/{_slug(row['title'])}",
                published_at=published,
                summary=row.get("summary"),
                paywalled=source.paywalled,
                is_official=source.is_official,
                fetched_at=now,
                extra=extra,
            )
        )

    indicators = [
        Indicator(**ind, source_name="BCRPData (muestra)", url="https://estadisticas.bcrp.gob.pe/estadisticas/series/")
        for ind in data.get("indicators", [])
    ]
    used = {i.source_id for i in items}
    runs = [
        SourceRun(
            source_id=s.id,
            source_name=s.name,
            connector=s.connectors[0] if s.connectors else None,
            status=SourceRunStatus.OK if s.id in used else SourceRunStatus.EMPTY,
            message="muestra",
        )
        for s in catalog.sources
    ]
    return IngestionResult(
        edition_date=edition_date,
        window=window,
        generated_at=now,
        sample=True,
        items=items,
        indicators=indicators,
        runs=runs,
    )

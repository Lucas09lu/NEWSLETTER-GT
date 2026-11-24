"""Orquestador de la ingesta híbrida → `raw_items_YYYY-MM-DD.json`.

Garantías:
- Aislamiento por fuente y por conector: cualquier excepción se registra en
  el log y en `runs[]` y el pipeline continúa con las demás fuentes.
- Los conectores `fallback_only` solo se ejecutan si los anteriores de esa
  fuente no devolvieron ítems (ahorra cuota de Brave).
- Dedup exacta por URL normalizada (la semántica la hace la Fase 2).
- En `dry_run` no se escribe estado persistente y la salida va a `out/`.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from config.settings import Settings
from config.sources_loader import SourceCatalog, load_sources
from core.http import build_client
from core.models import (
    ConnectorType,
    IngestionResult,
    Indicator,
    RawItem,
    Source,
    SourceRun,
    SourceRunStatus,
)
from core.state import JsonStateStore
from core.timewindow import edition_window
from scrapers.base import Connector, ConnectorContext, SkipConnector
from scrapers.bcrp_api import BcrpApiConnector
from scrapers.inbox_fetcher import InboxConnector
from scrapers.rss_fetcher import RssConnector
from scrapers.search_fetcher import BraveSearchConnector
from scrapers.static_index import StaticIndexConnector

log = logging.getLogger(__name__)

MAX_WORKERS = 6


def default_connectors() -> dict[ConnectorType, Connector]:
    return {
        ConnectorType.BCRP_API: BcrpApiConnector(),
        ConnectorType.RSS: RssConnector(),
        ConnectorType.INBOX: InboxConnector(),
        ConnectorType.SEARCH: BraveSearchConnector(),
        ConnectorType.STATIC_INDEX: StaticIndexConnector(),
    }


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #
def _fold(text: str) -> str:
    """minúsculas y sin tildes, para comparar vocabulario."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def build_keyword_regex(keywords: list[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    alternatives = "|".join(re.escape(_fold(k)) for k in sorted(set(keywords), key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})")


def matches_keywords(item: RawItem, regex: re.Pattern[str] | None) -> bool:
    if regex is None:
        return True
    return bool(regex.search(_fold(f"{item.title} {item.summary or ''}")))


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hint = " (bloqueo/anti-bot)" if code in (401, 403, 429) else ""
        return f"HTTP {code}{hint} en {exc.request.url}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timeout: {exc.request.url if exc.request else ''}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Ejecución por fuente
# --------------------------------------------------------------------------- #
def run_source(
    source: Source,
    ctx: ConnectorContext,
    connectors: dict[ConnectorType, Connector],
    keyword_regex: re.Pattern[str] | None,
    max_items: int,
) -> tuple[list[RawItem], list[Indicator], list[SourceRun]]:
    items: list[RawItem] = []
    indicators: list[Indicator] = []
    runs: list[SourceRun] = []

    if not source.connectors:
        runs.append(
            SourceRun(
                source_id=source.id,
                source_name=source.name,
                status=SourceRunStatus.SKIPPED,
                message="Fuente de consulta: sin monitoreo semanal",
            )
        )
        return items, indicators, runs

    for ctype in source.connectors:
        if ctype in source.fallback_only and items:
            continue  # el conector principal ya trajo resultados
        connector = connectors[ctype]
        started = time.monotonic()
        got: list[RawItem] = []
        try:
            output = connector.fetch(source, ctx)
            got = output.items
            if source.require_keywords:
                got = [i for i in got if matches_keywords(i, keyword_regex)]
            status = SourceRunStatus.OK if (got or output.indicators) else SourceRunStatus.EMPTY
            items.extend(got)
            indicators.extend(output.indicators)
            message = output.message
        except SkipConnector as exc:
            status, message = SourceRunStatus.SKIPPED, str(exc)
        except Exception as exc:  # aislamiento: nunca se propaga
            status, message = SourceRunStatus.ERROR, _describe_error(exc)
            log.warning(
                "Fuente #%s %s [%s] falló: %s",
                source.id,
                source.name,
                ctype.value,
                message,
                extra={"source_id": source.id, "connector": ctype.value, "status": "error"},
            )
        run = SourceRun(
            source_id=source.id,
            source_name=source.name,
            connector=ctype,
            status=status,
            items=len(got),
            message=message,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        runs.append(run)
        log.info(
            "#%-2s %-40s %-12s %-7s %s",
            source.id,
            source.name[:40],
            ctype.value,
            status.value,
            message or "",
            extra={"source_id": source.id, "connector": ctype.value, "status": status.value, "items": run.items},
        )

    # dedup dentro de la fuente + tope de ítems (más recientes primero)
    unique: dict[str, RawItem] = {}
    for item in items:
        unique.setdefault(item.id, item)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(unique.values(), key=lambda i: i.published_at or epoch, reverse=True)
    return ordered[:max_items], indicators, runs


def merge_items(items: list[RawItem]) -> list[RawItem]:
    """Dedup exacta entre fuentes, prefiriendo la fuente oficial."""
    best: dict[str, RawItem] = {}
    for item in items:
        current = best.get(item.id)
        if current is None or (item.is_official and not current.is_official):
            best[item.id] = item
    return list(best.values())


def update_source_health(state: JsonStateStore, runs: list[SourceRun], when: datetime) -> None:
    health: dict = state.load("source_health", {})
    by_source: dict[int, list[SourceRun]] = {}
    for run in runs:
        by_source.setdefault(run.source_id, []).append(run)
    for source_id, source_runs in by_source.items():
        entry = health.setdefault(str(source_id), {"consecutive_failures": 0})
        failed = all(r.status == SourceRunStatus.ERROR for r in source_runs)
        entry.update(
            {
                "name": source_runs[0].source_name,
                "last_run": when.isoformat(),
                "consecutive_failures": entry.get("consecutive_failures", 0) + 1 if failed else 0,
                "runs": [r.model_dump(mode="json", exclude={"source_id", "source_name"}) for r in source_runs],
            }
        )
    state.save("source_health", health)


# --------------------------------------------------------------------------- #
# Punto de entrada
# --------------------------------------------------------------------------- #
def run_ingestion(
    settings: Settings,
    edition_date: date,
    *,
    dry_run: bool = False,
    only_ids: set[int] | None = None,
    catalog: SourceCatalog | None = None,
    connectors: dict[ConnectorType, Connector] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> IngestionResult:
    catalog = catalog or load_sources(settings.sources_path)
    connectors = connectors or default_connectors()
    window = edition_window(edition_date)
    state = JsonStateStore(settings.data_path / "state", read_only=dry_run)
    sources = [s for s in catalog.sources if only_ids is None or s.id in only_ids]
    keyword_regex = build_keyword_regex(catalog.keywords)

    log.info(
        "Ingesta edición %s · ventana %s → %s · %d fuentes%s",
        edition_date,
        window.start.isoformat(),
        window.end.isoformat(),
        len(sources),
        " · DRY-RUN" if dry_run else "",
    )

    all_items: list[RawItem] = []
    indicators: list[Indicator] = []
    runs: list[SourceRun] = []
    with build_client(settings.http_timeout_seconds, transport=transport) as client:
        ctx = ConnectorContext(
            settings=settings,
            client=client,
            window=window,
            edition_date=edition_date,
            state=state,
            keywords=catalog.keywords,
        )
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [
                pool.submit(run_source, s, ctx, connectors, keyword_regex, catalog.max_items_per_source)
                for s in sources
            ]
            for future in futures:
                src_items, src_indicators, src_runs = future.result()
                all_items.extend(src_items)
                indicators.extend(src_indicators)
                runs.extend(src_runs)

    generated_at = datetime.now(timezone.utc)
    result = IngestionResult(
        edition_date=edition_date,
        window=window,
        generated_at=generated_at,
        items=merge_items(all_items),
        indicators=indicators,
        runs=sorted(runs, key=lambda r: r.source_id),
    )
    update_source_health(state, result.runs, generated_at)
    log.info("Resumen de ingesta: %s", result.summary())
    return result


def raw_items_path(settings: Settings, edition_date: date, dry_run: bool) -> Path:
    base = settings.output_path / "dry-run" if dry_run else settings.data_path / "raw"
    return base / f"raw_items_{edition_date.isoformat()}.json"


def write_result(result: IngestionResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_result(path: Path) -> IngestionResult:
    return IngestionResult.model_validate_json(path.read_text(encoding="utf-8"))

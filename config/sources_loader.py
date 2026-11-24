"""Lector de la base maestra `fuentes-comercio-exterior-peru.xlsx` (hoja `Fuentes`).

- Valida que existan las 10 columnas obligatorias del PRD §4.1.
- Convierte cada fila en un `Source` y le asigna la estrategia de ingesta:
  primero `source_overrides.yaml`; si la fuente no está ahí, se infiere de
  "Estado de la revisión" / "Nota" (ver `_default_strategy`).
- Filas incompletas se registran y se omiten; nunca detienen el pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
import yaml

from core.models import ConnectorType, Source
from core.urls import domain_of

log = logging.getLogger(__name__)

SHEET_NAME = "Fuentes"
OVERRIDES_PATH = Path(__file__).with_name("source_overrides.yaml")

# Encabezado del Excel → campo del modelo
REQUIRED_COLUMNS: dict[str, str] = {
    "#": "id",
    "Grupo": "group",
    "Tipo": "type",
    "Tipo de contenido": "content_type",
    "Nombre de la página": "name",
    "Descripción de la página": "description",
    "Link": "link",
    "Nota": "note",
    "Estado de la revisión": "review_status",
    "Cómo obtenerla": "how_to_get",
}


class SourcesFileError(RuntimeError):
    """El Excel no existe o no tiene la estructura mínima."""


@dataclass
class SourceCatalog:
    sources: list[Source]
    keywords: list[str]
    max_items_per_source: int
    warnings: list[str] = field(default_factory=list)

    def by_id(self, source_id: int) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)


def _load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"defaults": {}, "sources": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("defaults", {})
    data["sources"] = {int(k): (v or {}) for k, v in (data.get("sources") or {}).items()}
    return data


def _default_strategy(row: dict[str, Any]) -> dict[str, Any]:
    """Estrategia para fuentes nuevas que aún no están en el YAML."""
    status = (row.get("review_status") or "").lower()
    note = (row.get("note") or "").lower()
    domain = domain_of(row["link"])
    paywalled = "suscripción" in note or "suscripcion" in note
    if "no se pudo abrir" in status or "no visto" in status or paywalled:
        return {"connectors": ["search"], "search_query": f"site:{domain}", "paywalled": paywalled}
    return {
        "connectors": ["rss", "search"],
        "fallback_only": ["search"],
        "search_query": f"site:{domain}",
    }


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def load_sources(xlsx_path: Path, overrides_path: Path = OVERRIDES_PATH) -> SourceCatalog:
    if not xlsx_path.exists():
        raise SourcesFileError(f"No se encontró la base de fuentes: {xlsx_path}")

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise SourcesFileError(f"El Excel no tiene la hoja '{SHEET_NAME}' (hojas: {wb.sheetnames})")
    rows = list(wb[SHEET_NAME].iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise SourcesFileError("La hoja 'Fuentes' está vacía")

    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        raise SourcesFileError(f"Faltan columnas obligatorias en 'Fuentes': {missing}")
    index = {col: header.index(col) for col in REQUIRED_COLUMNS}

    overrides = _load_overrides(overrides_path)
    defaults = overrides["defaults"]
    warnings: list[str] = []
    sources: list[Source] = []
    seen_ids: set[int] = set()

    for line_no, raw in enumerate(rows[1:], start=2):
        if raw is None or all(v is None for v in raw):
            continue
        row = {
            field_: _clean(raw[index[col]]) if index[col] < len(raw) else None
            for col, field_ in REQUIRED_COLUMNS.items()
        }
        try:
            row["id"] = int(row["id"])
        except (TypeError, ValueError):
            warnings.append(f"Fila {line_no}: '#' inválido ({row['id']!r}); se omite")
            continue
        if not row.get("link") or not row.get("name"):
            warnings.append(f"Fuente #{row['id']}: sin Link o Nombre; se omite")
            continue
        if row["id"] in seen_ids:
            warnings.append(f"Fuente #{row['id']}: '#' duplicado; se omite la repetición")
            continue
        seen_ids.add(row["id"])

        strategy = overrides["sources"].get(row["id"])
        if strategy is None:
            strategy = _default_strategy(row)
            warnings.append(
                f"Fuente #{row['id']} ({row['name']}) sin override en YAML; "
                f"estrategia por defecto: {strategy['connectors']}"
            )

        note = (row.get("note") or "").lower()
        sources.append(
            Source(
                **{k: ("" if v is None and k != "note" else v) for k, v in row.items()},
                domain=domain_of(row["link"]),
                paywalled=bool(strategy.get("paywalled", "suscripción" in note)),
                connectors=[ConnectorType(c) for c in strategy.get("connectors", [])],
                fallback_only=[ConnectorType(c) for c in strategy.get("fallback_only", [])],
                feed_url=strategy.get("feed_url"),
                search_query=strategy.get("search_query"),
                inbox_senders=strategy.get("inbox_senders", []),
                index_url=strategy.get("index_url"),
                index_link_pattern=strategy.get("index_link_pattern"),
                require_keywords=bool(strategy.get("require_keywords", False)),
                bcrp_series=[str(s) for s in strategy.get("bcrp_series", [])],
                is_official=bool(strategy.get("official", False)),
            )
        )

    orphan = set(overrides["sources"]) - seen_ids
    if orphan:
        warnings.append(f"Overrides sin fila en el Excel (se ignoran): {sorted(orphan)}")

    for w in warnings:
        log.warning(w)
    log.info("Base de fuentes cargada: %d fuentes desde %s", len(sources), xlsx_path.name)
    return SourceCatalog(
        sources=sources,
        keywords=[k.lower() for k in defaults.get("keywords", [])],
        max_items_per_source=int(defaults.get("max_items_per_source", 40)),
        warnings=warnings,
    )

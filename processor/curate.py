"""Curación y análisis: raw items → `Edition` (edition_YYYY-MM-DD.json).

Flujo:
1. Prefiltro determinista (ventana + historial `sent_urls`).
2. Agrupación de duplicados + relevancia + pilar:
     - modo "claude": 1ª llamada a Claude (deduplicación semántica);
     - modo "heuristic": similitud de títulos + vocabulario (sin API key, o si
       Claude falla). La edición queda marcada para que se note la diferencia.
3. Selección: 10–15 noticias, máximo 3 por pilar, oficiales primero.
4. Redacción (titular / qué pasó / impacto + resumen ejecutivo): 2ª llamada a
   Claude. Fuente y enlace se asignan SIEMPRE por código desde el ítem
   original, nunca los escribe el modelo.
5. Validación anti-alucinación de cifras y armado del Radar de Cifras.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel

from config.settings import Settings
from core.edition import PILLAR_LABELS, Edition, EditionStats, Pillar, SourceRef, Story
from core.models import IngestionResult, RawItem, SourceRunStatus
from core.state import JsonStateStore
from core.timewindow import LIMA, date_range_label
from processor.llm import ClaudeClient, CurationError
from processor.prefilter import prefilter
from processor.radar import build_radar
from processor.validators import unsupported_numbers

log = logging.getLogger(__name__)

MAX_STORIES = 15
MIN_STORIES = 10
MAX_PER_PILLAR = 3
MAX_CANDIDATES = 20
HEADLINE_MAX_WORDS = 12


# =========================================================================== #
# Utilidades
# =========================================================================== #
def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").lower()) if not unicodedata.combining(c))


def trim_words(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]).rstrip(",;:") + "…"


@dataclass
class Cluster:
    items: list[RawItem]
    pillar: Pillar
    relevance: int
    draft: dict | None = None  # redacción provista por el modelo (o muestra)
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> RawItem:
        """Fuente primaria: oficial > con resumen > la que publicó primero (original)."""
        never = datetime.max.replace(tzinfo=timezone.utc)
        return min(
            self.items,
            key=lambda i: (not i.is_official, not i.summary, i.published_at or never),
        )

    def source_text(self) -> str:
        return "\n".join(f"{i.title}. {i.summary or ''}" for i in self.items)


# =========================================================================== #
# Modo heurístico (sin API key / respaldo)
# =========================================================================== #
PILLAR_KEYWORDS: dict[Pillar, list[str]] = {
    Pillar.LOGISTICA: ["flete", "naviera", "contenedor", "puerto", "portuari", "callao", "chancay", "teu",
                       "blank sailing", "carga aerea", "air cargo", "freight", "shipping", "container", "port",
                       "transit", "maritim", "logistic", "terminal", "buque"],
    Pillar.COMEX: ["exportacion", "importacion", "balanza comercial", "sunat", "bcrp", "inei", "adex",
                   "comexperu", "camara de comercio", "sni", "envios", "exportaron", "exportadores"],
    Pillar.REGULACION: ["norma", "decreto", "resolucion", "arancel", "aduan", "tlc", "acuerdo comercial",
                        "mincetur", "mtc", "tariff", "regulac", "el peruano", "reglamento", "certificado"],
    Pillar.SECTORIAL: ["agro", "arandano", "uva", "palta", "cafe", "cacao", "mango", "miner", "cobre", "oro",
                       "pesca", "textil", "manufactura", "campana", "fruta"],
    Pillar.GLOBAL: ["omc", "wto", "unctad", "cepal", "bid", "fmi", "geopolit", "global", "mundial",
                    "outlook", "barometer", "barometro", "aranceles de estados unidos", "china"],
}
GROUP_DEFAULT_PILLAR = {
    "Logística, puertos y carga": Pillar.LOGISTICA,
    "Sectoriales (agro, fruta, minería)": Pillar.SECTORIAL,
    "Organismos internacionales y bases de datos": Pillar.GLOBAL,
}
_STOP = set("para como sobre entre desde hasta este esta estos estas pero porque segun tras ante con por los las del una uno que mas the and for with from into".split())


def guess_pillar(item: RawItem) -> tuple[Pillar, int]:
    text = fold(f"{item.title} {item.summary or ''}")
    scores = {p: sum(text.count(k) for k in kws) for p, kws in PILLAR_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return GROUP_DEFAULT_PILLAR.get(item.source_group, Pillar.COMEX), 0
    return best, scores[best]


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", fold(text)) if w not in _STOP}


def heuristic_clusters(items: list[RawItem]) -> list[Cluster]:
    clusters: list[tuple[set[str], list[RawItem]]] = []
    for item in items:
        tokens = _tokens(item.title)
        for ctoks, members in clusters:
            union = tokens | ctoks
            if union and len(tokens & ctoks) / len(union) >= 0.45:
                members.append(item)
                ctoks |= tokens
                break
        else:
            clusters.append((tokens, [item]))

    result = []
    for _, members in clusters:
        pillar, hits = guess_pillar(members[0])
        relevance = min(5, 1 + hits + (1 if any(m.is_official for m in members) else 0) + (len(members) - 1))
        cluster = Cluster(items=members, pillar=pillar, relevance=relevance)
        sample = members[0].extra.get("sample_draft")
        if sample:
            cluster.draft = sample
            cluster.pillar = Pillar(sample.get("pillar", pillar))
            cluster.relevance = int(sample.get("relevance", relevance))
        result.append(cluster)
    return result


def heuristic_draft(cluster: Cluster) -> dict:
    if cluster.draft:
        return cluster.draft
    primary = cluster.primary
    summary = (primary.summary or "").strip()
    if len(summary) > 320:
        summary = summary[:320].rsplit(" ", 1)[0] + "…"
    return {
        "headline": trim_words(primary.title, HEADLINE_MAX_WORDS),
        "what_happened": summary or "Ver el detalle en la fuente original.",
        "impact": "Análisis comercial pendiente: se genera automáticamente al configurar CLAUDE_API_KEY.",
    }


# =========================================================================== #
# Modo Claude
# =========================================================================== #
PillarLiteral = Literal["logistica", "comex", "regulacion", "sectorial", "global"]

SYSTEM_PROMPT = """Eres el editor senior del boletín interno "Inteligencia Comex" de Global Transport, \
un agente de carga peruano miembro de la red WCA. Tus lectores son el equipo comercial: necesitan \
argumentos para cotizar, alertar a clientes y conversar con agentes de la red.

Pilares editoriales:
- logistica: Logística global, fletes y puertos (tarifas de contenedores, carga aérea, congestión, navieras, \
blank sailings, Callao, Chancay y la región).
- comex: Comercio exterior peruano y cifras macro (exportaciones, importaciones, balanza comercial, SUNAT, \
BCRP, INEI, ADEX, COMEXPERU, CCL, SNI).
- regulacion: Regulación, aduanas y acuerdos comerciales (normas en El Peruano, aranceles, barreras de acceso \
a EE. UU., UE y Asia, disposiciones de Mincetur/MTC/SUNAT).
- sectorial: Pulso sectorial (agroexportación: arándano, uva, palta, café, cacao; minería; manufactura no tradicional).
- global: Contexto global y tendencias (geopolítica con efecto comercial, OMC, UNCTAD, CEPAL, BID, indicadores adelantados).

Reglas no negociables:
1. Usa EXCLUSIVAMENTE la información de los ítems proporcionados. No agregues datos de tu conocimiento.
2. Prohibido inventar o recalcular cifras: cada número que escribas debe aparecer literalmente en el material.
3. Los ítems marcados paywall=true solo tienen titular y resumen público: no infieras lo que hay detrás.
4. Prioriza el impacto directo en tarifas, tiempos de tránsito, capacidad marítima/aérea, cambios aduaneros \
o demanda exportadora peruana. Descarta política general sin efecto logístico o comercial.
5. Español ejecutivo, directo y analítico. Mantén términos del sector en inglés cuando sea natural \
(TEU, blank sailings, freight rates, transit time, spot)."""


class ClusterOut(BaseModel):
    item_ids: list[str]
    pillar: PillarLiteral
    relevance: int
    reason: str


class ClusteringOut(BaseModel):
    clusters: list[ClusterOut]


class StoryDraftOut(BaseModel):
    cluster_id: int
    headline: str
    what_happened: str
    impact: str


class DraftOut(BaseModel):
    main_theme: str
    preheader: str
    executive_summary: list[str]
    stories: list[StoryDraftOut]


def _items_payload(items: list[RawItem], summary_chars: int) -> str:
    rows = []
    for i in items:
        rows.append(
            {
                "id": i.id,
                "fuente": i.source_name,
                "grupo": i.source_group,
                "oficial": i.is_official,
                "paywall": i.paywalled,
                "fecha": i.published_at.astimezone(LIMA).date().isoformat() if i.published_at else None,
                "titular": i.title,
                "resumen": (i.summary or "")[:summary_chars],
            }
        )
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def claude_clusters(client: ClaudeClient, items: list[RawItem]) -> list[Cluster]:
    prompt = (
        "Estos son los ítems recopilados esta semana (un JSON por línea):\n\n"
        f"{_items_payload(items, 300)}\n\n"
        "Tarea:\n"
        "- Agrupa en un mismo cluster los ítems que reportan el MISMO hecho (deduplicación semántica).\n"
        "- Asigna a cada cluster un pilar y una relevancia comercial de 1 (irrelevante) a 5 (crítica).\n"
        "- Devuelve SOLO clusters con relevancia >= 3, como máximo 30, ordenados de mayor a menor relevancia.\n"
        "- `reason`: una frase breve con el criterio usado.\n"
        "- Usa exactamente los `id` recibidos."
    )
    out = client.structured(system=SYSTEM_PROMPT, prompt=prompt, schema=ClusteringOut)
    by_id = {i.id: i for i in items}
    clusters = []
    used: set[str] = set()
    for c in out.clusters:
        members = [by_id[i] for i in c.item_ids if i in by_id and i not in used]
        if not members:
            continue
        used.update(m.id for m in members)
        clusters.append(Cluster(items=members, pillar=Pillar(c.pillar), relevance=max(1, min(5, c.relevance))))
    log.info("Claude agrupó %d ítems en %d clusters relevantes", len(items), len(clusters))
    return clusters


def claude_drafts(client: ClaudeClient, clusters: list[Cluster]) -> DraftOut:
    blocks = []
    for idx, cluster in enumerate(clusters):
        blocks.append(
            f"### cluster_id={idx} · pilar={cluster.pillar.value} · relevancia={cluster.relevance}\n"
            f"{_items_payload(cluster.items, 1200)}"
        )
    prompt = (
        "Redacta la edición semanal a partir de estos clusters de noticias ya seleccionados:\n\n"
        + "\n\n".join(blocks)
        + "\n\nPara cada cluster que tenga información suficiente, escribe:\n"
        f"- headline: titular concreto de máximo {HEADLINE_MAX_WORDS} palabras.\n"
        "- what_happened: 2 a 3 líneas con los hechos y cifras clave presentes en el material.\n"
        "- impact: 1 línea práctica sobre qué implica para el equipo comercial, los clientes o los agentes WCA.\n"
        "Omite un cluster si su material no alcanza para una nota útil (no rellenes).\n\n"
        "Además:\n"
        "- executive_summary: 3 a 4 bullets ('En 60 segundos') con lo más relevante de la semana.\n"
        "- main_theme: tema principal de la semana en máximo 6 palabras (va en el asunto del correo).\n"
        "- preheader: una línea con los 2-3 titulares más críticos.\n"
        "Recuerda: ninguna cifra que no esté literalmente en el material."
    )
    return client.structured(system=SYSTEM_PROMPT, prompt=prompt, schema=DraftOut)


# =========================================================================== #
# Selección y armado
# =========================================================================== #
def select_clusters(clusters: list[Cluster], limit: int, per_pillar: int) -> list[Cluster]:
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ranked = sorted(
        clusters,
        key=lambda c: (c.relevance, c.primary.is_official, len(c.items), c.primary.published_at or epoch),
        reverse=True,
    )
    chosen, per = [], {p: 0 for p in Pillar}
    for cluster in ranked:
        if per[cluster.pillar] >= per_pillar:
            continue
        chosen.append(cluster)
        per[cluster.pillar] += 1
        if len(chosen) >= limit:
            break
    return chosen


def build_story(cluster: Cluster, draft: dict) -> Story | None:
    primary = cluster.primary
    generated = " ".join(draft.get(k, "") for k in ("headline", "what_happened", "impact"))
    missing = unsupported_numbers(generated, cluster.source_text())
    if missing:
        log.warning("Nota descartada por cifras no verificables %s: %s", missing, draft.get("headline"))
        return None
    others: dict[str, SourceRef] = {}
    for item in cluster.items:
        if item.source_name != primary.source_name and item.source_name not in others:
            others[item.source_name] = SourceRef(name=item.source_name, url=item.url)
    return Story(
        id=primary.id,
        pillar=cluster.pillar,
        headline=trim_words(draft["headline"].strip(), HEADLINE_MAX_WORDS),
        what_happened=draft["what_happened"].strip(),
        impact=draft["impact"].strip(),
        source=SourceRef(name=primary.source_name, url=primary.url),
        also_reported_by=list(others.values()),
        published_at=primary.published_at,
        is_official=primary.is_official,
        paywalled=primary.paywalled,
        item_ids=[i.id for i in cluster.items],
    )


def curate(result: IngestionResult, settings: Settings, state: JsonStateStore, *, force_heuristic: bool = False) -> Edition:
    items = prefilter(result, state)
    notes: list[str] = []
    mode = "heuristic"
    drafts_out: DraftOut | None = None
    chosen: list[Cluster] = []

    client = None
    if not force_heuristic and not result.sample:
        try:
            client = ClaudeClient(settings)
        except CurationError as exc:
            notes.append(f"Curación heurística: {exc}.")

    if client and items:
        try:
            clusters = claude_clusters(client, items)
            chosen = select_clusters(clusters, MAX_CANDIDATES, MAX_PER_PILLAR + 1)
            drafts_out = claude_drafts(client, chosen)
            mode = "claude"
        except CurationError as exc:
            log.error("Curación con Claude falló, se usa el modo heurístico: %s", exc)
            notes.append(f"Curación con Claude no disponible ({exc}); edición armada en modo heurístico.")
            drafts_out = None

    stories: list[Story] = []
    if mode == "claude" and drafts_out is not None:
        for d in drafts_out.stories:
            if 0 <= d.cluster_id < len(chosen):
                story = build_story(chosen[d.cluster_id], d.model_dump())
                if story:
                    stories.append(story)
        # respeta el tope por pilar y el máximo total en el orden de relevancia
        per = {p: 0 for p in Pillar}
        capped = []
        for s in stories:
            if per[s.pillar] < MAX_PER_PILLAR and len(capped) < MAX_STORIES:
                capped.append(s)
                per[s.pillar] += 1
        stories = capped
        executive = drafts_out.executive_summary[:4]
        main_theme = drafts_out.main_theme
        preheader = drafts_out.preheader
    else:
        chosen = select_clusters(heuristic_clusters(items), MAX_STORIES, MAX_PER_PILLAR)
        for cluster in chosen:
            story = build_story(cluster, heuristic_draft(cluster))
            if story:
                stories.append(story)
        executive = [s.headline for s in stories[:4]]
        if not stories:
            main_theme = "Sin novedades relevantes"
        elif len(stories[0].headline.split()) <= 8:
            main_theme = stories[0].headline
        else:  # evita un asunto cortado a mitad de frase
            main_theme = PILLAR_LABELS[stories[0].pillar]
        preheader = " · ".join(s.headline for s in stories[:3])

    # orden final: por pilar editorial, conservando la relevancia interna
    pillar_rank = {p: i for i, p in enumerate(Pillar)}
    stories.sort(key=lambda s: pillar_rank[s.pillar])

    if len(stories) < MIN_STORIES:
        notes.append(f"Semana con pocas novedades verificables ({len(stories)} notas).")

    runs_by_source: dict[int, list] = {}
    for run in result.runs:
        runs_by_source.setdefault(run.source_id, []).append(run.status)
    stats = EditionStats(
        sources_total=len(runs_by_source),
        sources_ok=sum(1 for st in runs_by_source.values() if SourceRunStatus.OK in st),
        sources_error=sum(1 for st in runs_by_source.values() if all(s == SourceRunStatus.ERROR for s in st)),
        items_raw=len(result.items),
        items_considered=len(items),
    )
    range_label = date_range_label(result.window.start, result.window.end)
    return Edition(
        edition_date=result.edition_date,
        window=result.window,
        generated_at=datetime.now(timezone.utc),
        curation_mode=mode,
        sample=result.sample,
        subject=f"[Inteligencia Comex] Resumen Semanal · {range_label} | {main_theme}",
        preheader=preheader,
        main_theme=main_theme,
        executive_summary=executive,
        stories=stories,
        radar=build_radar(result.indicators),
        stats=stats,
        notes=notes,
    )


def edition_path(base, edition_date: date):
    return base / f"edition_{edition_date.isoformat()}.json"

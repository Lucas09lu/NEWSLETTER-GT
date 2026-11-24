import json

import pytest

from config.sources_loader import load_sources
from core.edition import Edition, Pillar
from core.models import ConnectorType, IngestionResult, RawItem
from core.state import JsonStateStore
from core.timewindow import edition_window
from delivery.render import render_email, render_text
from delivery.site import build_site
from processor import curate as curate_mod
from processor.prefilter import mark_sent, prefilter
from processor.sample import build_sample_result
from processor.validators import unsupported_numbers
from tests.conftest import EDITION, ROOT


@pytest.fixture
def catalog():
    return load_sources(ROOT / "data" / "fuentes-comercio-exterior-peru.xlsx")


@pytest.fixture
def sample(catalog):
    return build_sample_result(catalog, EDITION)


# --------------------------------------------------------------------- validador
@pytest.mark.parametrize(
    "generated, source, missing",
    [
        ("Subió 9% hasta USD 2,850 por FEU", "rose 9% to USD 2,850 per FEU", []),
        ("Superávit de US$ 2 100 millones", "superávit de US$ 2 100 millones", []),
        ("Movilizó 1,2 millones de TEU", "movilizó 1.2 millones de TEU", []),
        ("Exportaciones crecieron 14%", "exportaciones crecieron 11%", ["14"]),
        ("Vigente desde 2026 y en 3 etapas", "nada", []),  # años y números pequeños se ignoran
    ],
)
def test_unsupported_numbers(generated, source, missing):
    assert unsupported_numbers(generated, source) == missing


# --------------------------------------------------------------------- prefiltro
def test_prefilter_window_and_history(tmp_path, sample):
    state = JsonStateStore(tmp_path)
    first = sample.items[0]
    mark_sent(state, [first.url + "?utm_source=x"], EDITION)
    kept = prefilter(sample, state)
    assert first.id not in {i.id for i in kept}
    assert len(kept) == len(sample.items) - 1


def test_prefilter_accepts_undated_search_items(settings):
    window = edition_window(EDITION)
    common = dict(source_id=1, source_name="s", source_group="g", title="t", fetched_at=window.end)
    result = IngestionResult(
        edition_date=EDITION, window=window, generated_at=window.end,
        items=[
            RawItem(connector=ConnectorType.SEARCH, url="https://a.com/1", **common),
            RawItem(connector=ConnectorType.RSS, url="https://a.com/2", **common),
        ],
    )
    kept = prefilter(result, JsonStateStore(settings.data_path / "state", read_only=True))
    assert [i.url for i in kept] == ["https://a.com/1"]


# --------------------------------------------------------------------- curación
def test_sample_curation_heuristic(settings, sample, tmp_path):
    ed = curate_mod.curate(sample, settings, JsonStateStore(tmp_path, read_only=True))
    assert ed.sample and ed.curation_mode == "heuristic"
    assert 10 <= len(ed.stories) <= 15
    per = {p: sum(1 for s in ed.stories if s.pillar == p) for p in Pillar}
    assert max(per.values()) <= 3
    assert ed.subject.startswith("[Inteligencia Comex] Resumen Semanal · 23–29 nov 2026 | ")
    # el duplicado (Container News) quedó consolidado en la nota de Hellenic Shipping News
    fletes = next(s for s in ed.stories if "Fletes spot" in s.headline)
    assert fletes.source.name == "Hellenic Shipping News"
    assert [r.name for r in fletes.also_reported_by] == ["Container News"]
    assert len(ed.radar) == 3


class FakeClaude:
    """Simula las dos llamadas a Claude sin red."""

    def __init__(self, settings):
        self.calls = []

    def structured(self, *, system, prompt, schema, max_tokens=16000):
        self.calls.append(schema.__name__)
        ids = [line.split('"id": "')[1].split('"')[0] for line in prompt.splitlines() if '"id": "' in line]
        if schema is curate_mod.ClusteringOut:
            return schema(clusters=[
                {"item_ids": [ids[0], "id-inventado"], "pillar": "logistica", "relevance": 5, "reason": "tarifas"},
                {"item_ids": [ids[1]], "pillar": "comex", "relevance": 4, "reason": "cifras"},
            ])
        return schema(
            main_theme="Fletes al alza",
            preheader="Fletes suben",
            executive_summary=["Uno", "Dos", "Tres"],
            stories=[
                {"cluster_id": 0, "headline": "Fletes suben 9%", "what_happened": "Subieron 9%.", "impact": "Cotizar ya."},
                # cifra inventada → debe descartarse
                {"cluster_id": 1, "headline": "Dato inventado", "what_happened": "Creció 77%.", "impact": "x"},
                {"cluster_id": 9, "headline": "Cluster inexistente", "what_happened": "x", "impact": "x"},
            ],
        )


def test_claude_mode_with_fake_client(settings, sample, tmp_path, monkeypatch):
    sample.sample = False  # simular semana real
    monkeypatch.setattr(curate_mod, "ClaudeClient", FakeClaude)
    ed = curate_mod.curate(sample, settings, JsonStateStore(tmp_path, read_only=True))
    assert ed.curation_mode == "claude"
    assert [s.headline for s in ed.stories] == ["Fletes suben 9%"]
    assert ed.stories[0].source.url.startswith("https://example.com/muestra/")  # el enlace lo pone el código
    assert ed.main_theme == "Fletes al alza"


def test_claude_failure_falls_back_to_heuristic(settings, sample, tmp_path, monkeypatch):
    sample.sample = False

    class Broken:
        def __init__(self, settings):
            pass

        def structured(self, **kwargs):
            raise curate_mod.CurationError("sin cuota")

    monkeypatch.setattr(curate_mod, "ClaudeClient", Broken)
    ed = curate_mod.curate(sample, settings, JsonStateStore(tmp_path, read_only=True))
    assert ed.curation_mode == "heuristic" and ed.stories
    assert any("sin cuota" in n for n in ed.notes)


# --------------------------------------------------------------------- correo y sitio
@pytest.fixture
def edition(settings, sample, tmp_path) -> Edition:
    return curate_mod.curate(sample, settings, JsonStateStore(tmp_path, read_only=True))


def test_email_html_is_email_safe(edition):
    html = render_email(edition, app_base_url="https://x.github.io/app/")
    assert "<script" not in html.lower()
    assert "EDICIÓN DE MUESTRA" in html
    assert "#003978" in html and "#F26722" in html
    assert 'href="https://x.github.io/app/#/2026-11-30"' in html
    assert "https://x.github.io/app/assets/logo-global-transport.png" in html
    assert "Radar de Cifras" in html and "En 60 segundos" in html
    text = render_text(edition, app_base_url="https://x.github.io/app/")
    assert "EN 60 SEGUNDOS" in text


def test_build_site(tmp_path, edition):
    eds = tmp_path / "eds"
    eds.mkdir()
    (eds / "edition_2026-11-30.json").write_text(edition.model_dump_json(), encoding="utf-8")
    (eds / "edition_2026-11-23.json").write_text("{corrupto", encoding="utf-8")
    site = build_site(tmp_path / "site", [eds])
    index = json.loads((site / "data" / "index.json").read_text())
    assert [e["date"] for e in index["editions"]] == ["2026-11-30"]
    assert (site / "assets" / "logo-global-transport.png").exists()
    assert "__BUILD__" not in (site / "sw.js").read_text()
    assert (site / ".nojekyll").exists()

import json

from config.sources_loader import SourceCatalog
from core.models import ConnectorType, SourceRunStatus
from scrapers.base import Connector, ConnectorOutput, SkipConnector
from scrapers.orchestrator import raw_items_path, run_ingestion, write_result
from tests.conftest import EDITION, make_source


class FakeConnector(Connector):
    def __init__(self, ctype, behaviour):
        self.type = ctype
        self.behaviour = behaviour
        self.calls: list[int] = []

    def fetch(self, source, ctx):
        self.calls.append(source.id)
        result = self.behaviour(source)
        if isinstance(result, Exception):
            raise result
        items = [
            self.make_item(source, ctx, title=t, url=f"https://{source.domain}/{i}", published_at=ctx.window.start)
            for i, t in enumerate(result)
        ]
        return ConnectorOutput(items=items)


def _catalog(*sources):
    return SourceCatalog(sources=list(sources), keywords=["export", "flete"], max_items_per_source=40)


def test_failing_source_does_not_stop_pipeline(settings):
    rss = FakeConnector(ConnectorType.RSS, lambda s: RuntimeError("boom") if s.id == 1 else ["Fletes suben"])
    search = FakeConnector(ConnectorType.SEARCH, lambda s: SkipConnector("sin key"))
    catalog = _catalog(
        make_source(id=1, domain="a.com", connectors=[ConnectorType.RSS]),
        make_source(id=2, domain="b.com", connectors=[ConnectorType.RSS]),
        make_source(id=3, domain="c.com", connectors=[ConnectorType.SEARCH]),
        make_source(id=4, domain="d.com", connectors=[]),
    )
    result = run_ingestion(
        settings, EDITION, catalog=catalog, connectors={ConnectorType.RSS: rss, ConnectorType.SEARCH: search}
    )
    status = {r.source_id: r.status for r in result.runs}
    assert status == {
        1: SourceRunStatus.ERROR,
        2: SourceRunStatus.OK,
        3: SourceRunStatus.SKIPPED,
        4: SourceRunStatus.SKIPPED,
    }
    assert [i.source_id for i in result.items] == [2]
    health = json.loads((settings.data_path / "state" / "source_health.json").read_text())
    assert health["1"]["consecutive_failures"] == 1 and health["2"]["consecutive_failures"] == 0


def test_fallback_only_runs_when_primary_is_empty(settings):
    rss = FakeConnector(ConnectorType.RSS, lambda s: ["Exportaciones crecen"] if s.id == 1 else [])
    search = FakeConnector(ConnectorType.SEARCH, lambda s: ["Exportaciones vía búsqueda"])
    common = dict(connectors=[ConnectorType.RSS, ConnectorType.SEARCH], fallback_only=[ConnectorType.SEARCH])
    catalog = _catalog(make_source(id=1, domain="a.com", **common), make_source(id=2, domain="b.com", **common))
    run_ingestion(settings, EDITION, catalog=catalog, connectors={ConnectorType.RSS: rss, ConnectorType.SEARCH: search})
    assert search.calls == [2]


def test_keyword_filter_and_dry_run_has_no_state(settings):
    rss = FakeConnector(ConnectorType.RSS, lambda s: ["Exportación de café crece", "Resultado del fútbol"])
    catalog = _catalog(make_source(id=1, domain="a.com", connectors=[ConnectorType.RSS], require_keywords=True))
    result = run_ingestion(settings, EDITION, catalog=catalog, connectors={ConnectorType.RSS: rss}, dry_run=True)
    assert [i.title for i in result.items] == ["Exportación de café crece"]
    assert not (settings.data_path / "state").exists()
    path = write_result(result, raw_items_path(settings, EDITION, dry_run=True))
    assert path.parent == settings.output_path / "dry-run"
    assert json.loads(path.read_text())["window"]["start"].startswith("2026-11-23T00:00:00")


def test_duplicate_url_prefers_official_source(settings):
    rss = FakeConnector(ConnectorType.RSS, lambda s: ["Misma nota exportación"])
    catalog = _catalog(
        make_source(id=1, domain="same.com", connectors=[ConnectorType.RSS]),
        make_source(id=2, domain="same.com", connectors=[ConnectorType.RSS], is_official=True),
    )
    result = run_ingestion(settings, EDITION, catalog=catalog, connectors={ConnectorType.RSS: rss})
    assert len(result.items) == 1 and result.items[0].source_id == 2

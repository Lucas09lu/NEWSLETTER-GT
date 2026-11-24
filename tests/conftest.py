from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from core.models import Source  # noqa: E402
from core.state import JsonStateStore  # noqa: E402
from core.timewindow import edition_window  # noqa: E402
from scrapers.base import ConnectorContext  # noqa: E402

EDITION = date(2026, 11, 30)  # lunes → ventana 23-nov … 29-nov


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "out",
        sources_xlsx=ROOT / "data" / "fuentes-comercio-exterior-peru.xlsx",
        brave_api_key="test-key",
        brave_min_interval_seconds=0,
    )


@pytest.fixture
def ctx(settings, tmp_path):
    client = httpx.Client()
    yield ConnectorContext(
        settings=settings,
        client=client,
        window=edition_window(EDITION),
        edition_date=EDITION,
        state=JsonStateStore(tmp_path / "state"),
    )
    client.close()


def make_source(**overrides) -> Source:
    base = dict(
        id=99,
        group="Logística, puertos y carga",
        type="Medio",
        content_type="Noticias",
        name="Fuente de prueba",
        description="",
        link="https://example.com/noticias",
        review_status="Sin verificar",
        how_to_get="",
        domain="example.com",
    )
    base.update(overrides)
    return Source(**base)

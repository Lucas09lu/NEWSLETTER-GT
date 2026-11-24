from collections import Counter
from datetime import date, datetime

import openpyxl
import pytest

from config.sources_loader import SourcesFileError, load_sources
from core.models import ConnectorType
from core.timewindow import LIMA, edition_window, in_window
from core.urls import normalize_url
from tests.conftest import ROOT

XLSX = ROOT / "data" / "fuentes-comercio-exterior-peru.xlsx"


def test_loads_50_sources_with_prd_distribution():
    catalog = load_sources(XLSX)
    assert len(catalog.sources) == 50
    groups = Counter(s.group for s in catalog.sources)
    assert groups == {
        "Entidades oficiales (Perú)": 11,
        "Organismos internacionales y bases de datos": 11,
        "Prensa económica y de negocios": 10,
        "Logística, puertos y carga": 8,
        "Gremios y centros de estudio (Perú)": 6,
        "Sectoriales (agro, fruta, minería)": 4,
    }
    assert not [w for w in catalog.warnings if "sin override" in w]


def test_prd_ingestion_rules_are_mapped():
    catalog = load_sources(XLSX)
    by_id = {s.id: s for s in catalog.sources}
    assert by_id[7].connectors == [ConnectorType.BCRP_API] and by_id[7].bcrp_series
    assert by_id[36].feed_url
    # Boletines confirmados del PRD
    for sid in (15, 18, 20, 28, 34, 42):
        assert ConnectorType.INBOX in by_id[sid].connectors, sid
    # Paywall: solo titulares (y nunca RSS/cuerpo como conector principal)
    assert {s.id for s in catalog.sources if s.paywalled} == {18, 19, 33, 35}
    # Bloqueadas / sin RSS → búsqueda obligatoria
    for sid in (3, 6, 8, 27, 43, 49, 12, 14, 31, 37):
        assert by_id[sid].connectors[0] == ConnectorType.SEARCH, sid
    for sid in (38, 41, 42, 48):
        assert ConnectorType.STATIC_INDEX in by_id[sid].connectors, sid


def test_missing_column_raises(tmp_path):
    path = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fuentes"
    ws.append(["#", "Grupo", "Link"])
    wb.save(path)
    with pytest.raises(SourcesFileError, match="Faltan columnas"):
        load_sources(path)


def test_unknown_source_gets_default_strategy(tmp_path):
    path = tmp_path / "new.xlsx"
    wb = openpyxl.load_workbook(XLSX)
    ws = wb["Fuentes"]
    ws.append([51, "Logística, puertos y carga", "Medio", "x", "Nueva", "d", "https://nueva.com", None, "No se pudo abrir", "x"])
    wb.save(path)
    catalog = load_sources(path)
    new = catalog.by_id(51)
    assert new.connectors == [ConnectorType.SEARCH]
    assert new.search_query == "site:nueva.com"


def test_edition_window_monday():
    w = edition_window(date(2026, 11, 30))
    assert w.start == datetime(2026, 11, 23, 0, 0, tzinfo=LIMA)
    assert w.end == datetime(2026, 11, 29, 23, 59, 59, tzinfo=LIMA)
    # una ejecución manual el martes usa la misma semana cerrada
    assert edition_window(date(2026, 12, 1)) == w
    assert in_window(datetime(2026, 11, 29, 23, 0, tzinfo=LIMA), w)
    assert not in_window(datetime(2026, 11, 30, 0, 0, 1, tzinfo=LIMA), w)
    assert not in_window(None, w)


def test_normalize_url():
    a = normalize_url("http://www.Gestion.pe/economia/nota-123/?utm_source=nl&id=5#top")
    assert a == "https://gestion.pe/economia/nota-123?id=5"

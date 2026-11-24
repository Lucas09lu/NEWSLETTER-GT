from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
import pytest
import respx

from core.models import ConnectorType
from scrapers.base import SkipConnector
from scrapers.bcrp_api import BcrpApiConnector, parse_bcrp_response
from scrapers.inbox_fetcher import extract_links, parse_message, unwrap_tracking
from scrapers.rss_fetcher import RssConnector, SiteUnreachable, discover_feed
from scrapers.search_fetcher import BRAVE_ENDPOINT, BraveSearchConnector
from scrapers.static_index import StaticIndexConnector
from tests.conftest import make_source

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Fletes al alza en la ruta Asia–Callao</title><link>https://example.com/a</link>
<pubDate>Wed, 25 Nov 2026 10:00:00 -0500</pubDate><description>&lt;p&gt;Resumen &lt;b&gt;público&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Nota vieja</title><link>https://example.com/old</link>
<pubDate>Wed, 11 Nov 2026 10:00:00 -0500</pubDate></item>
<item><title>Nota del lunes de edición</title><link>https://example.com/late</link>
<pubDate>Mon, 30 Nov 2026 08:00:00 -0500</pubDate></item>
</channel></rss>"""


# --------------------------------------------------------------------------- RSS
@respx.mock
def test_rss_filters_to_window(ctx):
    respx.get("https://example.com/feed/").mock(return_value=httpx.Response(200, content=RSS))
    source = make_source(feed_url="https://example.com/feed/")
    out = RssConnector().fetch(source, ctx)
    assert [i.url for i in out.items] == ["https://example.com/a"]
    assert out.items[0].summary == "Resumen público"
    assert out.items[0].connector == ConnectorType.RSS


@respx.mock
def test_rss_discovery_via_link_alternate_and_cache(ctx):
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head></html>'
    page = respx.get("https://example.com/noticias").mock(return_value=httpx.Response(200, text=html))
    feed = respx.get("https://example.com/rss.xml").mock(return_value=httpx.Response(200, content=RSS))
    source = make_source()
    assert RssConnector().fetch(source, ctx).items
    cached = ctx.state.load("feeds_discovered")["99"]
    assert cached["url"] == "https://example.com/rss.xml"
    # segunda ejecución: usa la caché y no vuelve a sondear la página
    RssConnector().fetch(source, ctx)
    assert page.call_count == 1
    assert feed.call_count == 3  # sondeo + lectura (1ª) + lectura (2ª)


@respx.mock
def test_rss_discovery_unreachable_is_error_not_cached(ctx):
    respx.route(host="example.com").mock(return_value=httpx.Response(403))
    with pytest.raises(SiteUnreachable):
        RssConnector().fetch(make_source(), ctx)
    assert "99" not in ctx.state.load("feeds_discovered", {})


# --------------------------------------------------------------------------- BCRP
BCRP_PAYLOAD = {
    "config": {"title": "x", "series": [{"name": "Exportaciones (mill. US$)"}, {"name": "Importaciones (mill. US$)"}]},
    "periods": [
        {"name": "Ago.2026", "values": ["6500.1", "4800.5"]},
        {"name": "Set.2026", "values": ["6700.0", "n.d."]},
    ],
}


def test_bcrp_parse_handles_missing_values():
    inds = parse_bcrp_response(BCRP_PAYLOAD, ["A", "B"])
    assert inds[0].value == 6700.0 and inds[0].period == "Set.2026" and inds[0].previous_value == 6500.1
    assert inds[1].value == 4800.5 and inds[1].period == "Ago.2026" and inds[1].previous_value is None


@respx.mock
def test_bcrp_connector_html_error(ctx):
    respx.get(url__startswith="https://estadisticas.bcrp.gob.pe/").mock(
        return_value=httpx.Response(200, text="<html>error</html>")
    )
    with pytest.raises(RuntimeError, match="no devolvió JSON"):
        BcrpApiConnector().fetch(make_source(bcrp_series=["XX"]), ctx)


# --------------------------------------------------------------------------- Brave
@respx.mock
def test_search_filters_domain_window_and_institution(ctx):
    route = respx.get(BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Mincetur: <strong>exportaciones</strong> crecen", "url": "https://www.gob.pe/institucion/mincetur/noticias/1", "description": "snip", "page_age": "2026-11-25T12:00:00"},
                        {"title": "Otra institución", "url": "https://www.gob.pe/institucion/minsa/noticias/2"},
                        {"title": "Otro dominio", "url": "https://otro.com/x"},
                        {"title": "Fuera de ventana", "url": "https://www.gob.pe/institucion/mincetur/noticias/3", "page_age": "2026-10-01T00:00:00"},
                        {"title": "Sin fecha", "url": "https://www.gob.pe/institucion/mincetur/noticias/4"},
                    ]
                }
            },
        )
    )
    source = make_source(link="https://www.gob.pe/institucion/mincetur/noticias", domain="gob.pe", search_query="site:gob.pe mincetur")
    out = BraveSearchConnector().fetch(source, ctx)
    assert [i.title for i in out.items] == ["Mincetur: exportaciones crecen", "Sin fecha"]
    assert out.items[1].extra["date_from"] == "search_freshness"
    params = route.calls.last.request.url.params
    assert params["freshness"] == "2026-11-23to2026-11-29"
    assert route.calls.last.request.headers["X-Subscription-Token"] == "test-key"


def test_search_skips_without_key(ctx):
    ctx.settings.brave_api_key = None
    with pytest.raises(SkipConnector):
        BraveSearchConnector().fetch(make_source(), ctx)


def test_paywall_summary_is_truncated(ctx):
    item = BraveSearchConnector().make_item(make_source(paywalled=True), ctx, title="t", url="https://e.com", summary="x" * 2000)
    assert len(item.summary) == 400 and item.paywalled


# --------------------------------------------------------------------------- Inbox
def _newsletter() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Agraria.pe <boletin@agraria.pe>"
    msg["Subject"] = "Boletín diario"
    msg["Date"] = "Thu, 26 Nov 2026 07:00:00 -0500"
    msg.set_content("texto")
    msg.add_alternative(
        """<html><body><table><tr><td>
        <a href="https://agraria.pe/noticias/arandano-peruano-supera-las-300-mil-toneladas">Arándano peruano supera las 300 mil toneladas exportadas</a>
        <p>Detalle de la campaña.</p></td></tr></table>
        <a href="https://click.mail.com/r?url=https%3A%2F%2Fagraria.pe%2Fnoticias%2Fuva-de-mesa-abre-mercado-en-india">Uva de mesa peruana abre nuevo mercado en India</a>
        <a href="https://agraria.pe/unsubscribe?x=1">Darse de baja de este boletín informativo</a>
        <a href="https://facebook.com/agraria">Síguenos en Facebook para más noticias</a>
        <a href="https://agraria.pe">Ver</a>
        </body></html>""",
        subtype="html",
    )
    return msg.as_bytes()


def test_inbox_extracts_editorial_links():
    message = parse_message(_newsletter())
    assert message.sender == "boletin@agraria.pe"
    links = extract_links(message)
    assert [u for _, u, _ in links] == [
        "https://agraria.pe/noticias/arandano-peruano-supera-las-300-mil-toneladas",
        "https://agraria.pe/noticias/uva-de-mesa-abre-mercado-en-india",
    ]


def test_unwrap_tracking_keeps_plain_urls():
    assert unwrap_tracking("https://agraria.pe/x?id=1") == "https://agraria.pe/x?id=1"


# --------------------------------------------------------------------------- Static index
@respx.mock
def test_static_index_baseline_then_new(ctx):
    page1 = '<a href="/reports/air-cargo-market-analysis-september-2026/">Sept</a>'
    page2 = page1 + '<a href="/reports/air-cargo-market-analysis-october-2026/">Air Cargo Market Analysis – October 2026</a>'
    route = respx.get("https://iata.test/econ").mock(return_value=httpx.Response(200, text=page1))
    source = make_source(index_url="https://iata.test/econ", index_link_pattern="air-cargo-market-analysis")
    first = StaticIndexConnector().fetch(source, ctx)
    assert first.items == [] and "línea base" in first.message
    route.mock(return_value=httpx.Response(200, text=page2))
    second = StaticIndexConnector().fetch(source, ctx)
    assert [i.title for i in second.items] == ["Air Cargo Market Analysis – October 2026"]
    assert second.items[0].published_at is None
    assert datetime.fromisoformat(second.items[0].extra["first_seen"]) <= datetime.now(timezone.utc)

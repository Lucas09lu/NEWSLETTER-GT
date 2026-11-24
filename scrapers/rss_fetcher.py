"""Conector RSS/Atom con descubrimiento automático de feeds y sitemaps.

Orden de descubrimiento (si la fuente no tiene `feed_url` confirmado):
1. `<link rel="alternate" type="application/rss+xml|atom+xml">` en la página.
2. Rutas comunes: /feed/, /rss, /feed, /rss.xml, /atom.xml (WordPress, etc.).
3. Sitemaps de noticias declarados en robots.txt (`<lastmod>` o
   `<news:publication_date>`), solo si 1 y 2 no encontraron nada.

El resultado se cachea en `data/state/feeds_discovered.json`: positivo 30 días,
negativo 7 días, para no sondear 29 sitios cada semana.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from core.http import get
from core.models import ConnectorType, RawItem, Source
from core.timewindow import in_window
from scrapers.base import Connector, ConnectorContext, ConnectorOutput

log = logging.getLogger(__name__)

STATE_KEY = "feeds_discovered"
POSITIVE_TTL = timedelta(days=30)
NEGATIVE_TTL = timedelta(days=7)
COMMON_FEED_PATHS = ("/feed/", "/rss", "/feed", "/rss.xml", "/atom.xml", "/index.xml")
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
SITEMAP_HINT = re.compile(r"(news|post|noticia|article)", re.I)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    for key in ("published", "updated", "dc_date"):
        value = entry.get(key)
        if value:
            try:
                dt = dateparser.parse(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True) or None


def parse_feed(content: bytes) -> feedparser.FeedParserDict:
    return feedparser.parse(content)


def is_valid_feed(parsed: feedparser.FeedParserDict) -> bool:
    return bool(parsed.get("entries")) and parsed.get("version", "") != ""


# --------------------------------------------------------------------------- #
# Descubrimiento
# --------------------------------------------------------------------------- #
def _try_feed(client: httpx.Client, url: str) -> bool:
    # Sin reintentos: son sondeos baratos y la mayoría de rutas no existen.
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return is_valid_feed(parse_feed(response.content))


class SiteUnreachable(RuntimeError):
    """La página de la fuente no se pudo abrir (bloqueo, timeout, DNS)."""


def discover_feed(client: httpx.Client, page_url: str) -> str | None:
    """Devuelve la URL de un feed válido para `page_url` o None.

    Lanza `SiteUnreachable` si la página no se pudo abrir y ningún candidato
    respondió, para que el fallo se registre como error y no se cachee como
    "sin feed" (podría ser un bloqueo transitorio).
    """
    candidates: list[str] = []
    page_error: Exception | None = None
    try:
        html = get(client, page_url).text
        soup = BeautifulSoup(html, "lxml")
        for link in soup.find_all("link", rel=lambda r: r and "alternate" in r):
            if (link.get("type") or "").lower() in FEED_TYPES and link.get("href"):
                candidates.append(urljoin(page_url, link["href"]))
    except Exception as exc:
        page_error = exc
        log.debug("No se pudo leer %s para descubrir feed: %s", page_url, exc)

    root = f"{httpx.URL(page_url).scheme}://{httpx.URL(page_url).host}"
    # Feeds de sección primero (p. ej. /economia/feed/) y luego los de la raíz.
    section = page_url.rstrip("/")
    if section != root:
        candidates += [section + p for p in COMMON_FEED_PATHS[:2]]
    candidates += [root + p for p in COMMON_FEED_PATHS]

    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if _try_feed(client, url):
            return url
    if page_error is not None:
        raise SiteUnreachable(f"No se pudo abrir {page_url}: {page_error}") from page_error
    return None


def discover_news_sitemap(client: httpx.Client, page_url: str) -> str | None:
    root = f"{httpx.URL(page_url).scheme}://{httpx.URL(page_url).host}"
    try:
        robots = get(client, f"{root}/robots.txt").text
    except Exception:
        return None
    sitemaps = [line.split(":", 1)[1].strip() for line in robots.splitlines() if line.lower().startswith("sitemap:")]
    preferred = [s for s in sitemaps if SITEMAP_HINT.search(s)]
    return (preferred or [None])[0]


def parse_sitemap(content: bytes) -> list[tuple[str, datetime | None, str | None]]:
    """(url, fecha, título) de un sitemap XML (incluye extensión news:)."""
    soup = BeautifulSoup(content, "xml")
    out: list[tuple[str, datetime | None, str | None]] = []
    for node in soup.find_all("url"):
        loc = node.find("loc")
        if not loc or not loc.text:
            continue
        raw_date = node.find("publication_date") or node.find("lastmod")
        dt = None
        if raw_date and raw_date.text:
            try:
                dt = dateparser.parse(raw_date.text.strip())
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                dt = None
        title = node.find("title")
        out.append((loc.text.strip(), dt, title.text.strip() if title and title.text else None))
    return out


def pick_child_sitemap(content: bytes) -> str | None:
    """Si `content` es un <sitemapindex>, devuelve el hijo de noticias más reciente."""
    soup = BeautifulSoup(content, "xml")
    if not soup.find("sitemapindex"):
        return None
    children: list[tuple[str, str]] = []
    for node in soup.find_all("sitemap"):
        loc = node.find("loc")
        if loc and loc.text:
            lastmod = node.find("lastmod")
            children.append((lastmod.text.strip() if lastmod and lastmod.text else "", loc.text.strip()))
    preferred = [c for c in children if SITEMAP_HINT.search(c[1])] or children
    return max(preferred)[1] if preferred else None


def _title_from_url(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(html?|php)$", "", slug)
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\b\d{5,}\b", "", slug).strip()
    return slug[:1].upper() + slug[1:] if slug else url


# --------------------------------------------------------------------------- #
# Conector
# --------------------------------------------------------------------------- #
class RssConnector(Connector):
    type = ConnectorType.RSS

    def _resolve(self, source: Source, ctx: ConnectorContext) -> tuple[str | None, str | None]:
        """Devuelve (tipo, url) con tipo ∈ {"feed", "sitemap"} usando caché."""
        if source.feed_url:
            return "feed", source.feed_url

        cache: dict = ctx.state.load(STATE_KEY, {})
        entry = cache.get(str(source.id))
        if entry:
            checked = datetime.fromisoformat(entry["checked_at"])
            ttl = POSITIVE_TTL if entry.get("url") else NEGATIVE_TTL
            if ctx.now - checked < ttl and entry.get("link") == source.link:
                return entry.get("kind"), entry.get("url")

        kind, url = None, discover_feed(ctx.client, source.link)
        if url:
            kind = "feed"
        else:
            url = discover_news_sitemap(ctx.client, source.link)
            kind = "sitemap" if url else None

        cache[str(source.id)] = {
            "kind": kind,
            "url": url,
            "link": source.link,
            "checked_at": ctx.now.isoformat(),
        }
        ctx.state.save(STATE_KEY, cache)
        log.info("Descubrimiento #%s %s → %s", source.id, source.name, url or "sin feed")
        return kind, url

    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput:
        kind, url = self._resolve(source, ctx)
        if not url:
            return ConnectorOutput(message="No se encontró RSS ni sitemap de noticias")
        response = get(ctx.client, url)
        items = (
            self._from_feed(source, ctx, response.content)
            if kind == "feed"
            else self._from_sitemap(source, ctx, response.content)
        )
        return ConnectorOutput(items=items, message=f"{kind}: {url}")

    def _from_feed(self, source: Source, ctx: ConnectorContext, content: bytes) -> list[RawItem]:
        parsed = parse_feed(content)
        if parsed.get("bozo") and not parsed.get("entries"):
            raise RuntimeError(f"Feed inválido: {parsed.get('bozo_exception')}")
        items: list[RawItem] = []
        for entry in parsed.entries:
            link, title = entry.get("link"), _strip_html(entry.get("title"))
            published = _entry_datetime(entry)
            if not link or not title or not in_window(published, ctx.window):
                continue
            items.append(
                self.make_item(
                    source,
                    ctx,
                    title=title,
                    url=link,
                    published_at=published,
                    summary=_strip_html(entry.get("summary") or entry.get("description")),
                )
            )
        return items

    def _from_sitemap(self, source: Source, ctx: ConnectorContext, content: bytes) -> list[RawItem]:
        child = pick_child_sitemap(content)
        if child:  # es un índice de sitemaps: bajar al sitemap hijo más reciente
            content = get(ctx.client, child).content
        items: list[RawItem] = []
        for url, published, title in parse_sitemap(content):
            if in_window(published, ctx.window):
                items.append(
                    self.make_item(source, ctx, title=title or _title_from_url(url), url=url, published_at=published)
                )
        return items

"""Conector de boletines recibidos por correo (IMAP).

Una cuenta dedicada se suscribe a los boletines gratuitos confirmados (CCL,
Andina, Agraria.pe, gCaptain, UNCTAD, Gestión). Cada semana:
1. Se descargan UNA sola vez los correos de la ventana (SINCE/BEFORE), en
   modo solo lectura (no se marcan como leídos).
2. Cada fuente toma los correos cuyo remitente coincide con `inbox_senders`.
3. De cada boletín se extraen los enlaces a notas (texto del ancla = titular).

No se siguen los enlaces de tracking (registraría un clic); solo se
desenvuelven los que llevan la URL destino como parámetro.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from core.models import ConnectorType, RawItem, Source
from core.timewindow import LIMA, in_window
from scrapers.base import Connector, ConnectorContext, ConnectorOutput, SkipConnector

log = logging.getLogger(__name__)

_SKIP_LINK = re.compile(
    r"(unsubscribe|desuscrib|darse de baja|cancelar suscrip|preferenc|ver en (el )?navegador|view in browser|"
    r"facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|youtube\.com|whatsapp|mailto:|tel:)",
    re.I,
)
_REDIRECT_KEYS = ("url", "u", "redirect", "redirect_url", "target", "dest", "destination", "link")


@dataclass
class InboxMessage:
    sender: str
    subject: str
    date: datetime | None
    html: str | None
    text: str | None


def unwrap_tracking(url: str) -> str:
    """Si la URL es un redirect con el destino en la query, devuelve el destino."""
    query = parse_qs(urlsplit(url).query)
    for key in _REDIRECT_KEYS:
        for value in query.get(key, []):
            candidate = unquote(value)
            if candidate.startswith(("http://", "https://")):
                return candidate
    return url


def parse_message(raw: bytes) -> InboxMessage:
    msg: EmailMessage = email.message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    try:
        date = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
    except (TypeError, ValueError):
        date = None
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    return InboxMessage(
        sender=sender,
        subject=str(msg.get("Subject", "")).strip(),
        date=date,
        html=html_part.get_content() if html_part else None,
        text=text_part.get_content() if text_part else None,
    )


def extract_links(message: InboxMessage, min_len: int = 25, max_len: int = 220) -> list[tuple[str, str, str | None]]:
    """(titular, url, contexto) de los enlaces editoriales de un boletín HTML."""
    if not message.html:
        return []
    soup = BeautifulSoup(message.html, "lxml")
    seen: set[str] = set()
    out: list[tuple[str, str, str | None]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        text = " ".join(anchor.get_text(" ", strip=True).split())
        if not href.startswith(("http://", "https://")) or _SKIP_LINK.search(href) or _SKIP_LINK.search(text):
            continue
        if not (min_len <= len(text) <= max_len):
            continue
        url = unwrap_tracking(href)
        if url in seen:
            continue
        seen.add(url)
        context = None
        parent = anchor.find_parent(["td", "p", "div", "li"])
        if parent is not None:
            ctx_text = " ".join(parent.get_text(" ", strip=True).split())
            if ctx_text and ctx_text != text:
                context = ctx_text[:500]
        out.append((text, url, context))
    return out


class InboxConnector(Connector):
    type = ConnectorType.INBOX

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: list[InboxMessage] | None = None

    def _load_messages(self, ctx: ConnectorContext) -> list[InboxMessage]:
        with self._lock:
            if self._messages is not None:
                return self._messages
            s = ctx.settings
            since = ctx.window.start.astimezone(LIMA).date()
            before = ctx.window.end.astimezone(LIMA).date() + timedelta(days=1)
            criteria = f'(SINCE "{since:%d-%b-%Y}" BEFORE "{before:%d-%b-%Y}")'
            messages: list[InboxMessage] = []
            with imaplib.IMAP4_SSL(s.newsletter_inbox_imap, s.newsletter_inbox_port, timeout=s.http_timeout_seconds) as imap:
                imap.login(s.newsletter_inbox_user, s.newsletter_inbox_pass.get_secret_value())
                imap.select(s.newsletter_inbox_folder, readonly=True)
                status, data = imap.search(None, criteria)
                if status != "OK":
                    raise RuntimeError(f"IMAP SEARCH falló: {status}")
                ids = data[0].split()
                log.info("Buzón de boletines: %d correos en la ventana", len(ids))
                for msg_id in ids:
                    status, payload = imap.fetch(msg_id, "(BODY.PEEK[])")
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        continue
                    try:
                        messages.append(parse_message(payload[0][1]))
                    except Exception as exc:  # un correo malformado no frena el resto
                        log.warning("Correo %s no se pudo parsear: %s", msg_id, exc)
            self._messages = messages
            return messages

    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput:
        if not ctx.settings.has_inbox:
            raise SkipConnector("Buzón IMAP no configurado (NEWSLETTER_INBOX_*)")
        if not source.inbox_senders:
            raise SkipConnector("Fuente sin remitentes de boletín configurados")
        messages = [
            m
            for m in self._load_messages(ctx)
            if any(m.sender.endswith(sender.lower()) for sender in source.inbox_senders)
        ]
        items: list[RawItem] = []
        for message in messages:
            date = message.date or ctx.now
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if not in_window(date, ctx.window):
                continue
            for title, url, context in extract_links(message):
                items.append(
                    self.make_item(
                        source,
                        ctx,
                        title=title,
                        url=url,
                        published_at=date,
                        summary=context,
                        extra={"newsletter_subject": message.subject, "date_from": "email_date"},
                    )
                )
        return ConnectorOutput(items=items, message=f"{len(messages)} boletines")

"""Envío del newsletter por SMTP, con modo `--dry-run` que nunca envía.

En dry-run se escriben `email.html` (para abrir en el navegador) y
`email.eml` (para abrir con Outlook/Apple Mail y ver exactamente el correo).
Las ediciones de MUESTRA nunca se envían, aunque no se pida dry-run.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from config.settings import Settings
from core.edition import Edition

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    pass


def build_message(edition: Edition, html: str, text: str, settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = edition.subject
    msg["From"] = formataddr((settings.sender_name, settings.sender_email or "no-configurado@example.com"))
    msg["To"] = ", ".join(settings.recipient_list)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=(settings.sender_email or "gt.local").split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def write_preview(msg: EmailMessage, html: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "email.html"
    eml_path = out_dir / "email.eml"
    html_path.write_text(html, encoding="utf-8")
    eml_path.write_bytes(bytes(msg))
    return html_path, eml_path


def send(msg: EmailMessage, settings: Settings) -> None:
    if not settings.has_smtp:
        raise DeliveryError("SMTP no configurado (SMTP_HOST, SENDER_EMAIL, RECIPIENTS)")
    password = settings.smtp_pass.get_secret_value() if settings.smtp_pass else None
    context = ssl.create_default_context()
    try:
        if settings.smtp_starttls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
                smtp.starttls(context=context)
                if settings.smtp_user and password:
                    smtp.login(settings.smtp_user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=60, context=context) as smtp:
                if settings.smtp_user and password:
                    smtp.login(settings.smtp_user, password)
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"Fallo de envío SMTP: {exc}") from exc
    log.info("Correo enviado a %s", msg["To"])

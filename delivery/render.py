"""Renderiza `Edition` → HTML de correo (tablas + estilos inline) y texto plano."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.edition import Edition
from core.timewindow import LIMA, date_range_label, long_date

TEMPLATES = Path(__file__).resolve().parent / "templates"
ASSETS = Path(__file__).resolve().parent / "assets"
LOGO_FILE = "logo-global-transport.png"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def app_link(app_base_url: str, edition: Edition) -> str:
    return f"{app_base_url.rstrip('/')}/#/{edition.edition_date.isoformat()}"


def render_email(edition: Edition, *, app_base_url: str, logo_url: str | None = None) -> str:
    cutoff = edition.window.end.astimezone(LIMA)
    notice = None
    if edition.curation_mode != "claude" and not edition.sample:
        notice = (
            "Edición armada en modo automático básico: titulares y resúmenes tomados de las fuentes; "
            "el análisis de impacto se activa al configurar la API de Claude."
        )
    return _env.get_template("newsletter.html.j2").render(
        edition=edition,
        range_label=date_range_label(edition.window.start, edition.window.end),
        edition_label=long_date(edition.edition_date),
        cutoff_label=f"domingo {long_date(cutoff.date())}, 23:59",
        app_url=app_link(app_base_url, edition),
        logo_url=logo_url or f"{app_base_url.rstrip('/')}/assets/{LOGO_FILE}",
        notice=notice,
    )


def render_text(edition: Edition, *, app_base_url: str) -> str:
    """Alternativa en texto plano (mejora entregabilidad y accesibilidad)."""
    lines = []
    if edition.sample:
        lines += ["*** EDICIÓN DE MUESTRA · contenido ficticio, no reenviar ***", ""]
    lines += [
        "GLOBAL TRANSPORT · INTELIGENCIA COMEX",
        f"Resumen semanal {date_range_label(edition.window.start, edition.window.end)} | {edition.main_theme}",
        "",
        "EN 60 SEGUNDOS",
    ]
    lines += [f"  • {b}" for b in edition.executive_summary]
    for _, label, stories in edition.stories_by_pillar():
        lines += ["", label.upper(), "-" * len(label)]
        for s in stories:
            lines += [
                f"■ {s.headline}",
                f"  {s.what_happened}",
                f"  Impacto: {s.impact}",
                f"  Fuente: {s.source.name} — {s.source.url}",
                "",
            ]
    if edition.radar:
        lines += ["RADAR DE CIFRAS"]
        lines += [f"  {r.label}: {r.value} ({r.period}) {r.change or ''}".rstrip() for r in edition.radar]
    lines += ["", f"Leer en la App / ediciones anteriores: {app_link(app_base_url, edition)}", "",
              "Uso interno de Global Transport."]
    return "\n".join(lines)

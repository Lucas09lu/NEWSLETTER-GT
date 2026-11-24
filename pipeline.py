"""CLI del sistema de Inteligencia Comex de Global Transport.

Uso rápido:
    python pipeline.py run --dry-run --sample   # vista previa completa con datos de MUESTRA (sin red)
    python pipeline.py run --dry-run            # pipeline real sin enviar correo ni tocar data/state
    python pipeline.py preview                  # sirve la app y el correo en http://localhost:8000
    python pipeline.py sources                  # fuentes y estrategia asignada

Comandos por capa: ingest → curate → email → build-site (ver --help de cada uno).

`--dry-run` nunca envía correos ni modifica `data/state/`; todo queda en `out/`.
"""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import socketserver
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import typer

from config.settings import Settings, get_settings
from config.sources_loader import SourcesFileError, load_sources
from core.edition import Edition
from core.logging_setup import setup_logging
from core.state import JsonStateStore
from core.timewindow import edition_window, today_lima
from scrapers.orchestrator import load_result, raw_items_path, run_ingestion, write_result

app = typer.Typer(add_completion=False, help="Newsletter & PWA de Inteligencia Comex — Global Transport")


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #
class Paths:
    """Dónde va cada artefacto según el modo (real vs dry-run)."""

    def __init__(self, settings: Settings, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.out = settings.output_path
        self.base = self.out / "dry-run" if dry_run else settings.data_path
        self.editions = self.base / "editions"
        self.real_editions = settings.data_path / "editions"
        self.email_dir = self.out / "dry-run" if dry_run else self.out / "email"
        self.site = self.out / "site"
        self.state = settings.data_path / "state"

    def raw(self, settings: Settings, d: date) -> Path:
        return raw_items_path(settings, d, self.dry_run)

    def edition(self, d: date) -> Path:
        return self.editions / f"edition_{d.isoformat()}.json"


def _parse_date(value: str | None) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else today_lima()


def _parse_ids(value: str | None) -> set[int] | None:
    return {int(v) for v in value.split(",") if v.strip()} if value else None


def _init(run_name: str, dry_run: bool) -> Settings:
    settings = get_settings()
    log_dir = settings.output_path / "logs" if dry_run else settings.data_path.parent / "logs"
    setup_logging(settings.log_level, log_dir, run_name)
    return settings


def _load_edition(path: Path) -> Edition:
    if not path.exists():
        typer.secho(f"No existe {path}. Ejecuta antes el paso anterior.", fg="red")
        raise typer.Exit(1)
    return Edition.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
@app.command()
def sources() -> None:
    """Lista las fuentes del Excel y la estrategia de ingesta asignada."""
    settings = get_settings()
    setup_logging("WARNING")
    try:
        catalog = load_sources(settings.sources_path)
    except SourcesFileError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    for s in catalog.sources:
        flags = [f for f, on in (("paywall", s.paywalled), ("oficial", s.is_official), ("filtro-kw", s.require_keywords)) if on]
        conns = " → ".join(f"{c.value}{'*' if c in s.fallback_only else ''}" for c in s.connectors) or "(consulta)"
        typer.echo(f"#{s.id:<3} {s.name[:42]:<42} {conns:<34} {' '.join(flags)}")
    counter = Counter(c.value for s in catalog.sources for c in s.connectors)
    typer.echo(f"\n{len(catalog.sources)} fuentes · conectores: {dict(counter)} · (*) = solo si lo anterior no trae nada")
    typer.echo(
        "Credenciales: "
        f"Claude={'sí' if settings.has_claude else 'no'} · Brave={'sí' if settings.has_brave else 'no'} · "
        f"IMAP={'sí' if settings.has_inbox else 'no'} · SMTP={'sí' if settings.has_smtp else 'no'}"
    )


@app.command()
def window(edition: str = typer.Option(None, "--date")) -> None:
    """Muestra la ventana editorial (lunes→domingo, hora Lima) de una edición."""
    w = edition_window(_parse_date(edition))
    typer.echo(f"{w.start.isoformat()} → {w.end.isoformat()}")


@app.command()
def ingest(
    edition: str = typer.Option(None, "--date", help="Fecha de edición YYYY-MM-DD (por defecto: hoy, hora Lima)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No modifica data/state; salida en out/"),
    only: str = typer.Option(None, "--only", help="IDs de fuente separados por coma, p. ej. 7,36"),
    sample: bool = typer.Option(False, "--sample", help="Usa la semana de MUESTRA (sin red; implica --dry-run)"),
) -> None:
    """Capa 2: ingesta híbrida → raw_items_YYYY-MM-DD.json"""
    dry_run = dry_run or sample
    d = _parse_date(edition)
    settings = _init(f"ingest_{d}", dry_run)
    if sample:
        from processor.sample import build_sample_result

        result = build_sample_result(load_sources(settings.sources_path), d)
    else:
        result = run_ingestion(settings, d, dry_run=dry_run, only_ids=_parse_ids(only))
    path = write_result(result, Paths(settings, dry_run).raw(settings, d))
    s = result.summary()
    typer.secho(f"✔ raw items: {path}", fg="green")
    typer.echo(
        f"  ítems={s['items']} · indicadores={s['indicators']} · ok={s['ok']} · vacías={s['empty']} · "
        f"omitidas={s['skipped']} · errores={s['error']}" + (" · MUESTRA" if sample else "")
    )


@app.command()
def curate(
    edition: str = typer.Option(None, "--date"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    heuristic: bool = typer.Option(False, "--heuristic", help="No usar Claude (modo heurístico)"),
) -> None:
    """Capa 3: curación y análisis → edition_YYYY-MM-DD.json"""
    from processor.curate import curate as run_curate

    d = _parse_date(edition)
    settings = _init(f"curate_{d}", dry_run)
    paths = Paths(settings, dry_run)
    raw = paths.raw(settings, d)
    if not raw.exists():
        typer.secho(f"No existe {raw}. Ejecuta antes `ingest`.", fg="red")
        raise typer.Exit(1)
    result = load_result(raw)
    state = JsonStateStore(paths.state, read_only=True)  # la curación solo lee el historial
    ed = run_curate(result, settings, state, force_heuristic=heuristic)
    out = paths.edition(d)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ed.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.secho(f"✔ edición: {out}", fg="green")
    typer.echo(f"  modo={ed.curation_mode} · noticias={len(ed.stories)} · radar={len(ed.radar)}")
    typer.echo(f"  asunto: {ed.subject}")
    for note in ed.notes:
        typer.secho(f"  nota: {note}", fg="yellow")


@app.command()
def email(
    edition: str = typer.Option(None, "--date"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Genera email.html y email.eml sin enviar"),
) -> None:
    """Capa 4a: renderiza el correo HTML y lo envía por SMTP (o lo previsualiza)."""
    from delivery.render import ASSETS, LOGO_FILE, render_email, render_text
    from delivery.send import DeliveryError, build_message, send, write_preview
    from processor.prefilter import mark_sent

    d = _parse_date(edition)
    settings = _init(f"email_{d}", dry_run)
    paths = Paths(settings, dry_run)
    ed = _load_edition(paths.edition(d))

    text = render_text(ed, app_base_url=settings.app_base_url)
    html_remote = render_email(ed, app_base_url=settings.app_base_url)
    msg = build_message(ed, html_remote, text, settings)

    if dry_run or ed.sample:
        # La vista local usa el logo copiado al lado del HTML (funciona sin internet).
        html_local = render_email(ed, app_base_url=settings.app_base_url, logo_url=LOGO_FILE)
        html_path, eml_path = write_preview(msg, html_local, paths.email_dir)
        shutil.copy2(ASSETS / LOGO_FILE, html_path.parent / LOGO_FILE)
        typer.secho(f"✔ vista previa (NO enviado): {html_path}", fg="green")
        typer.echo(f"  .eml para abrir en Outlook/Apple Mail: {eml_path}")
        if ed.sample and not dry_run:
            typer.secho("  Las ediciones de muestra nunca se envían.", fg="yellow")
        return
    try:
        send(msg, settings)
    except DeliveryError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    mark_sent(JsonStateStore(paths.state), [r for s in ed.stories for r in [s.source.url, *(x.url for x in s.also_reported_by)]], d)
    typer.secho(f"✔ enviado a {', '.join(settings.recipient_list)}", fg="green")


@app.command("build-site")
def build_site_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Incluye ediciones de out/dry-run y la vista del correo"),
) -> None:
    """Capa 4b: genera la PWA estática en out/site (lista para GitHub Pages)."""
    from delivery.site import build_site, build_standalone

    settings = _init("build_site", dry_run)
    paths = Paths(settings, dry_run)
    dirs = [paths.real_editions] + ([paths.editions] if dry_run else [])
    preview = (paths.email_dir / "email.html") if dry_run else None
    dest = build_site(paths.site, dirs, email_preview=preview)
    typer.secho(f"✔ sitio: {dest}", fg="green")
    single = build_standalone(dest, paths.out / "vista-previa-web.html")
    typer.secho(f"✔ versión de un solo archivo (doble clic para abrir): {single}", fg="green")


@app.command()
def preview(port: int = typer.Option(8000, "--port")) -> None:
    """Sirve out/site en http://localhost:PORT (app + vista del correo)."""
    settings = get_settings()
    site = Paths(settings, True).site
    if not (site / "index.html").exists():
        typer.secho("No hay sitio generado. Ejecuta: python pipeline.py run --dry-run --sample", fg="red")
        raise typer.Exit(1)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    with socketserver.TCPServer(("", port), handler) as httpd:
        typer.secho(f"App:    http://localhost:{port}/", fg="green")
        if (site / "preview" / "email.html").exists():
            typer.secho(f"Correo: http://localhost:{port}/preview/email.html", fg="green")
        typer.echo("Ctrl+C para detener.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


@app.command()
def run(
    edition: str = typer.Option(None, "--date"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No envía correo ni modifica data/state"),
    sample: bool = typer.Option(False, "--sample", help="Datos de MUESTRA sin red (implica --dry-run)"),
    heuristic: bool = typer.Option(False, "--heuristic", help="No usar Claude"),
) -> None:
    """Pipeline completo: ingesta → curación → correo → sitio PWA."""
    dry_run = dry_run or sample
    ingest(edition=edition, dry_run=dry_run, only=None, sample=sample)
    curate(edition=edition, dry_run=dry_run, heuristic=heuristic)
    if dry_run:
        email(edition=edition, dry_run=True)
        build_site_cmd(dry_run=True)
        typer.secho("\nListo. Para verlo: python pipeline.py preview", fg="cyan", bold=True)
    else:
        # Orden del PRD §9: primero se publica la app, luego se envía el correo.
        build_site_cmd(dry_run=False)
        email(edition=edition, dry_run=False)


if __name__ == "__main__":
    sys.exit(app())

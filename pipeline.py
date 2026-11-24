"""CLI del sistema de Inteligencia Comex de Global Transport.

Uso rápido:
    python pipeline.py sources                 # ver fuentes y estrategia asignada
    python pipeline.py ingest --dry-run        # ingesta sin tocar el estado
    python pipeline.py ingest --only 7,36      # probar fuentes concretas
    python pipeline.py run --dry-run           # pipeline completo en modo prueba

`--dry-run` nunca envía correos ni modifica `data/state/`; todo lo que genera
queda en `out/`.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime

import typer

from config.settings import get_settings
from config.sources_loader import SourcesFileError, load_sources
from core.logging_setup import setup_logging
from core.timewindow import edition_window, today_lima
from scrapers.orchestrator import raw_items_path, run_ingestion, write_result

app = typer.Typer(add_completion=False, help="Newsletter & PWA de Inteligencia Comex — Global Transport")


def _parse_date(value: str | None) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else today_lima()


def _parse_ids(value: str | None) -> set[int] | None:
    if not value:
        return None
    return {int(v) for v in value.split(",") if v.strip()}


def _init(run_name: str, dry_run: bool):
    settings = get_settings()
    log_dir = settings.output_path / "logs" if dry_run else settings.resolve(settings.data_dir).parent / "logs"
    log_file = setup_logging(settings.log_level, log_dir, run_name)
    return settings, log_file


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
        flags = []
        if s.paywalled:
            flags.append("paywall")
        if s.is_official:
            flags.append("oficial")
        if s.require_keywords:
            flags.append("filtro-kw")
        conns = " → ".join(
            f"{c.value}{'*' if c in s.fallback_only else ''}" for c in s.connectors
        ) or "(consulta)"
        typer.echo(f"#{s.id:<3} {s.name[:42]:<42} {conns:<34} {' '.join(flags)}")
    counter = Counter(c.value for s in catalog.sources for c in s.connectors)
    typer.echo(f"\n{len(catalog.sources)} fuentes · conectores: {dict(counter)} · (*) = solo si lo anterior no trae nada")
    typer.echo(
        "Credenciales: "
        f"Claude={'sí' if settings.has_claude else 'no'} · Brave={'sí' if settings.has_brave else 'no'} · "
        f"IMAP={'sí' if settings.has_inbox else 'no'} · SMTP={'sí' if settings.has_smtp else 'no'}"
    )


@app.command()
def ingest(
    edition: str = typer.Option(None, "--date", help="Fecha de edición YYYY-MM-DD (por defecto: hoy, hora Lima)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No modifica data/state; salida en out/"),
    only: str = typer.Option(None, "--only", help="IDs de fuente separados por coma, p. ej. 7,36"),
) -> None:
    """Capa 2: ingesta híbrida → raw_items_YYYY-MM-DD.json"""
    edition_date = _parse_date(edition)
    settings, log_file = _init(f"ingest_{edition_date}", dry_run)
    result = run_ingestion(settings, edition_date, dry_run=dry_run, only_ids=_parse_ids(only))
    path = write_result(result, raw_items_path(settings, edition_date, dry_run))
    summary = result.summary()
    typer.secho(f"\n✔ raw items: {path}", fg="green")
    typer.echo(
        f"  ítems={summary['items']} · indicadores={summary['indicators']} · "
        f"ok={summary['ok']} · vacías={summary['empty']} · omitidas={summary['skipped']} · errores={summary['error']}"
    )
    if log_file:
        typer.echo(f"  log: {log_file}")


@app.command()
def window(edition: str = typer.Option(None, "--date")) -> None:
    """Muestra la ventana editorial (lunes→domingo, hora Lima) de una edición."""
    w = edition_window(_parse_date(edition))
    typer.echo(f"{w.start.isoformat()} → {w.end.isoformat()}")


@app.command()
def run(
    edition: str = typer.Option(None, "--date"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Pipeline completo. (Fase 1: solo ingesta; las capas siguientes se añaden por fases.)"""
    ingest(edition=edition, dry_run=dry_run, only=None)


if __name__ == "__main__":
    sys.exit(app())

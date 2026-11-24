"""Construye el sitio estático de la PWA (listo para GitHub Pages).

Estructura generada:
    <dest>/index.html, app.js, styles.css, sw.js, manifest.json, icons/
    <dest>/assets/logo-global-transport.png
    <dest>/data/index.json                     ← lista de ediciones
    <dest>/data/edition_YYYY-MM-DD.json        ← una por semana
    <dest>/preview/email.html                  ← (solo dry-run) vista del correo
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.edition import Edition
from core.timewindow import date_range_label
from delivery.render import ASSETS, LOGO_FILE

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


def collect_editions(dirs: list[Path]) -> dict[str, Path]:
    """fecha → archivo; los directorios posteriores tienen prioridad."""
    found: dict[str, Path] = {}
    for d in dirs:
        if d.exists():
            for f in sorted(d.glob("edition_*.json")):
                found[f.stem.removeprefix("edition_")] = f
    return found


def build_site(dest: Path, edition_dirs: list[Path], email_preview: Path | None = None) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(WEBAPP_DIR, dest)
    (dest / "assets").mkdir(exist_ok=True)
    shutil.copy2(ASSETS / LOGO_FILE, dest / "assets" / LOGO_FILE)

    # versión del service worker = momento del build (fuerza actualización del shell)
    sw = dest / "sw.js"
    build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    sw.write_text(sw.read_text(encoding="utf-8").replace("__BUILD__", build_id), encoding="utf-8")

    data_dir = dest / "data"
    data_dir.mkdir()
    index = []
    for date_key, path in sorted(collect_editions(edition_dirs).items(), reverse=True):
        try:
            edition = Edition.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # una edición corrupta no rompe el sitio
            log.warning("Edición inválida %s: %s", path.name, exc)
            continue
        shutil.copy2(path, data_dir / path.name)
        index.append(
            {
                "date": date_key,
                "subject": edition.subject,
                "main_theme": edition.main_theme,
                "range_label": date_range_label(edition.window.start, edition.window.end),
                "stories": len(edition.stories),
                "sample": edition.sample,
                "curation_mode": edition.curation_mode,
            }
        )
    (data_dir / "index.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "editions": index}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (dest / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: servir tal cual

    if email_preview and email_preview.exists():
        preview = dest / "preview"
        preview.mkdir()
        shutil.copy2(email_preview, preview / "email.html")
        shutil.copy2(ASSETS / LOGO_FILE, preview / LOGO_FILE)

    log.info("Sitio PWA generado en %s (%d ediciones)", dest, len(index))
    return dest


def build_standalone(site_dir: Path, dest: Path) -> Path:
    """Empaqueta el sitio en UN solo .html (CSS, JS, logo y ediciones incrustados).

    Sirve para abrirlo con doble clic, adjuntarlo o revisarlo sin publicar nada.
    No incluye service worker ni instalación (eso requiere el sitio publicado).
    """
    html = (site_dir / "index.html").read_text(encoding="utf-8")
    css = (site_dir / "styles.css").read_text(encoding="utf-8")
    js = (site_dir / "app.js").read_text(encoding="utf-8")

    def data_uri(path: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    data = {}
    for f in sorted((site_dir / "data").glob("*.json")):
        data[f"data/{f.name}"] = json.loads(f.read_text(encoding="utf-8"))
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<link rel="manifest" href="manifest.json">\n', "")
    html = html.replace('src="assets/logo-global-transport.png"', f'src="{data_uri(site_dir / "assets" / LOGO_FILE)}"')
    html = html.replace('href="icons/favicon.png"', f'href="{data_uri(site_dir / "icons" / "favicon.png")}"')
    html = html.replace('<script src="app.js" defer></script>', f"<script>window.__GT_DATA__ = {payload};</script>\n<script>\n{js}\n</script>")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return dest

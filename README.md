# Global Transport · Inteligencia Comex

Sistema automatizado de curación semanal de noticias de comercio internacional y
logística para **Global Transport** (Perú): newsletter HTML los lunes 07:30 (hora
Lima) a `wca.sales@gt.com.pe` + Web App / PWA móvil con el archivo de ediciones.

> Estado: **Fases 1–4 implementadas** (ingesta, curación, correo, PWA) + botón de
> vista previa en GitHub Actions. Falta la Fase 5: ejecución automática de los
> lunes y la guía completa de credenciales.

## Ver el sistema funcionando

### Opción A — Sin instalar nada (GitHub Actions)

1. En GitHub abre la pestaña **Actions** → **Vista previa (manual, sin envío)** → **Run workflow**.
2. Elige el modo:
   - `muestra`: datos ficticios para ver el diseño del correo y la app (no usa internet ni claves).
   - `real`: lee las fuentes reales de la última semana. Usa los *Secrets* que existan
     (`BRAVE_API_KEY`, `CLAUDE_API_KEY`, `NEWSLETTER_INBOX_*`); los conectores sin clave se omiten.
3. Al terminar (~2 min):
   - **App:** https://lucas09lu.github.io/NEWSLETTER-GT/ (ábrela en el celular → “Agregar a pantalla de inicio”).
   - **Correo:** https://lucas09lu.github.io/NEWSLETTER-GT/preview/email.html
   - **Resumen por fuente:** en la página de la ejecución (estado de cada una de las 50 fuentes).
   - **Archivos:** sección *Artifacts* → `vista-previa-…` (JSON, `email.html`, `email.eml`).

Este flujo **nunca envía correos**.

### Opción B — En tu computadora

```bash
python pipeline.py run --dry-run --sample    # muestra, sin internet ni claves
python pipeline.py preview                   # abre http://localhost:8000
```

`python pipeline.py run --dry-run` hace lo mismo con las fuentes reales.

## Arquitectura

```
config/      settings (.env) · lector del Excel maestro · source_overrides.yaml
core/        modelos Pydantic · ventana Lima · HTTP · estado JSON · logging
scrapers/    bcrp_api · rss_fetcher (+descubrimiento) · inbox_fetcher (IMAP)
             search_fetcher (Brave) · static_index · orchestrator
processor/   prefiltro · curación con Claude (o heurística) · validador de cifras · radar
delivery/    plantilla Jinja2 del correo · envío SMTP · build del sitio PWA
webapp/      PWA estática (HTML + JS + CSS, sin build)
data/        Excel maestro · raw/ · editions/ · state/
```

### Estrategia de ingesta

Cada fuente del Excel (hoja `Fuentes`) recibe una cadena de conectores definida en
[`config/source_overrides.yaml`](config/source_overrides.yaml). Puedes verla con:

```bash
python pipeline.py sources
```

| Conector | Fuentes | Notas |
|---|---|---|
| `bcrp_api` | #7 BCRPData | Series para el *Radar de Cifras* (nombres tomados de la API) |
| `rss` | 18 fuentes | Feed confirmado (#36) o descubierto (`<link rel=alternate>`, rutas comunes, sitemap de noticias); caché 30 días |
| `inbox` | CCL, Gestión, Andina, Agraria.pe, gCaptain, UNCTAD | IMAP solo lectura; extrae los enlaces del boletín |
| `search` | 42 (22 principales + fallbacks) | Brave Search `site:` con `freshness` = ventana exacta; nunca abre el artículo |
| `static_index` | IATA, OMC, UNCTAD, BID INTAL | Vigila la página índice y emite solo informes nuevos |

Reglas del PRD implementadas en la ingesta:

- **Ventana estricta:** lunes 00:00 → domingo 23:59:59 hora Lima anterior a la edición.
- **Anti-paywall:** Gestión, El Comercio, The Loadstar y JOC solo aportan titular,
  fecha y resumen público (recortado a 400 caracteres). Nunca se pide el cuerpo.
- **Resiliencia:** cada fuente y conector corre aislado; un 403, timeout o error de
  parseo se registra en el log y en `data/state/source_health.json`
  (con `consecutive_failures`) y el pipeline continúa.
- Si falta una credencial, el conector queda como `skipped`, no como error.

## Uso local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # completa las credenciales que tengas
```

```bash
python pipeline.py sources                          # fuentes + estrategia + credenciales detectadas
python pipeline.py window --date 2026-11-30         # ventana editorial de una edición
python pipeline.py ingest --dry-run                 # ingesta completa sin tocar data/state
python pipeline.py ingest --dry-run --only 7,36     # probar fuentes concretas
python pipeline.py curate --dry-run                 # raw items → edition_YYYY-MM-DD.json
python pipeline.py email --dry-run                  # correo → out/dry-run/email.html + .eml
python pipeline.py build-site --dry-run             # PWA → out/site
python -m pytest                                    # tests (HTTP y Claude simulados, sin red)
```

## Curación (Fase 2)

- **Con `CLAUDE_API_KEY`:** dos llamadas a Claude con salida estructurada. La primera agrupa
  duplicados (deduplicación semántica), asigna pilar y puntúa relevancia comercial; la segunda
  redacta titular, “qué pasó”, impacto, “En 60 segundos”, tema y preheader.
  La fuente y el enlace de cada nota los asigna el código desde el ítem original, nunca el modelo.
- **Sin clave, o si Claude falla:** modo heurístico (titulares y resúmenes de las fuentes, sin
  análisis de impacto). El correo y la app lo indican.
- **Regla anti-alucinación:** toda cifra del texto generado debe aparecer en el material de
  origen; si no, la nota se descarta.
- Máximo 15 notas y 3 por pilar. El *Radar de Cifras* sale de BCRPData sin pasar por la IA.
- Las URLs ya publicadas se guardan en `data/state/sent_urls.json` y no se repiten.

`--dry-run` escribe en `out/dry-run/raw_items_YYYY-MM-DD.json` y `out/logs/`, y no
modifica el estado persistente.

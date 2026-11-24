# Global Transport · Inteligencia Comex

Sistema automatizado de curación semanal de noticias de comercio internacional y
logística para **Global Transport** (Perú): newsletter HTML los lunes 07:30 (hora
Lima) a `wca.sales@gt.com.pe` + Web App / PWA móvil con el archivo de ediciones.

> Estado: **Fase 1 (estructura + ingesta híbrida)** implementada. Las fases 2–5
> (curación con Claude, plantilla de correo, PWA y GitHub Actions) se añaden por
> etapas; este README se completará con la guía de credenciales en la Fase 5.

## Arquitectura

```
config/      settings (.env) · lector del Excel maestro · source_overrides.yaml
core/        modelos Pydantic · ventana Lima · HTTP · estado JSON · logging
scrapers/    bcrp_api · rss_fetcher (+descubrimiento) · inbox_fetcher (IMAP)
             search_fetcher (Brave) · static_index · orchestrator
processor/   (Fase 2) curación con Claude → edition_YYYY-MM-DD.json
delivery/    (Fase 3) plantilla Jinja2 + envío SMTP · assets/logo
webapp/      (Fase 4) PWA estática
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
python -m pytest                                    # tests (HTTP simulado, sin red)
```

`--dry-run` escribe en `out/dry-run/raw_items_YYYY-MM-DD.json` y `out/logs/`, y no
modifica el estado persistente.

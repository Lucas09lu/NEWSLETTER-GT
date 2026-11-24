"""Conector BCRPData (API oficial del Banco Central de Reserva del Perú).

Documentación: https://estadisticas.bcrp.gob.pe/estadisticas/series/ayuda/api
Formato: /api/{código1-código2}/json/{periodo_ini}/{periodo_fin}/esp

Devuelve `Indicator` (último valor disponible + anterior) para el Radar de
Cifras. Los nombres de las series se toman de la respuesta de la API para no
inventar etiquetas. No filtra por ventana semanal: las cifras mensuales se
publican con rezago y el Radar muestra "el último dato disponible".
"""

from __future__ import annotations

import logging
from datetime import date

from core.http import get
from core.models import ConnectorType, Indicator, Source
from scrapers.base import Connector, ConnectorContext, ConnectorOutput

log = logging.getLogger(__name__)

API_BASE = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SERIES_PAGE = "https://estadisticas.bcrp.gob.pe/estadisticas/series/mensuales/resultados/{code}/html"


def _period_param(d: date) -> str:
    return f"{d.year}-{d.month}"


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:  # "n.d." u otros marcadores de dato no disponible
        return None


def parse_bcrp_response(payload: dict, codes: list[str]) -> list[Indicator]:
    series_meta = payload.get("config", {}).get("series", [])
    periods = payload.get("periods", [])
    indicators: list[Indicator] = []
    for idx, code in enumerate(codes):
        name = series_meta[idx].get("name", code) if idx < len(series_meta) else code
        points: list[tuple[str, float]] = []
        for period in periods:
            values = period.get("values") or []
            value = _to_float(values[idx]) if idx < len(values) else None
            if value is not None:
                points.append((period.get("name", "?"), value))
        if not points:
            log.warning("BCRPData: la serie %s no tiene datos en el rango", code)
            continue
        last_period, last_value = points[-1]
        prev_period, prev_value = points[-2] if len(points) > 1 else (None, None)
        indicators.append(
            Indicator(
                series_code=code,
                name=name,
                period=last_period,
                value=last_value,
                previous_period=prev_period,
                previous_value=prev_value,
                url=SERIES_PAGE.format(code=code),
            )
        )
    return indicators


class BcrpApiConnector(Connector):
    type = ConnectorType.BCRP_API

    def fetch(self, source: Source, ctx: ConnectorContext) -> ConnectorOutput:
        codes = source.bcrp_series
        if not codes:
            return ConnectorOutput(message="Sin series configuradas")
        end = ctx.edition_date
        start = date(end.year - 1, end.month, 1)  # 13 meses para tener dato previo
        url = f"{API_BASE}/{'-'.join(codes)}/json/{_period_param(start)}/{_period_param(end)}/esp"
        response = get(ctx.client, url)
        try:
            payload = response.json()
        except ValueError as exc:  # la API devuelve HTML cuando un código es inválido
            raise RuntimeError(f"BCRPData no devolvió JSON (¿código de serie inválido?): {url}") from exc
        indicators = parse_bcrp_response(payload, codes)
        return ConnectorOutput(indicators=indicators, message=f"{len(indicators)} series")

"""Ventana temporal editorial en hora Lima (UTC-5, sin horario de verano).

Regla del PRD §5: procesar solo lo publicado del lunes anterior 00:00 al
domingo 23:59:59 (hora Lima). Para una edición del lunes 30-nov-2026, la
ventana es 23-nov 00:00 → 29-nov 23:59:59.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from core.models import TimeWindow

LIMA = ZoneInfo("America/Lima")


def today_lima() -> date:
    return datetime.now(LIMA).date()


def edition_window(edition_date: date) -> TimeWindow:
    """Semana calendario cerrada (lun-dom) inmediatamente anterior a `edition_date`.

    Si `edition_date` es lunes, el domingo de cierre es el día anterior. Si se
    ejecuta otro día (p. ej. una prueba manual en martes), se usa la última
    semana completa terminada antes de esa fecha.
    """
    days_back_to_sunday = edition_date.weekday() + 1  # lun→1, mar→2, …, dom→7
    end_day = edition_date - timedelta(days=days_back_to_sunday)
    start_day = end_day - timedelta(days=6)
    start = datetime.combine(start_day, time.min, tzinfo=LIMA)
    end = datetime.combine(end_day, time(23, 59, 59), tzinfo=LIMA)
    return TimeWindow(start=start, end=end)


def in_window(dt: datetime | None, window: TimeWindow) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return window.start <= dt <= window.end

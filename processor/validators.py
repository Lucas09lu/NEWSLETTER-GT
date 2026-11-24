"""Regla anti-alucinación (PRD §5): toda cifra debe existir en el texto fuente.

Se extraen los números del texto generado (titular, qué pasó, impacto) y se
comprueba que cada uno aparezca en el material de origen del cluster (títulos
y resúmenes públicos). Se toleran separadores distintos ("6.700" ≈ "6,700"
≈ "6700") y se ignoran años (1990–2035) y ordinales pequeños (≤ 10).
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _canonical(token: str) -> set[str]:
    """Formas equivalentes de un número: sin separadores y con decimal normalizado."""
    digits = re.sub(r"[.,]", "", token)
    forms = {digits}
    # "6,5" / "6.5" → "6.5" (decimal); "6.700" → "6700" (miles)
    if re.fullmatch(r"\d+[.,]\d{1,2}", token):
        forms.add(token.replace(",", "."))
    return forms


def _ignorable(token: str) -> bool:
    digits = re.sub(r"[.,]", "", token)
    if not digits.isdigit():
        return True
    value = int(digits)
    if len(digits) == 4 and 1990 <= value <= 2035 and not re.search(r"[.,]", token):
        return True  # año
    return value <= 10 and len(digits) <= 2


def numbers_in(text: str) -> set[str]:
    return {tok.rstrip(".,") for tok in _NUMBER.findall(text or "")}


def unsupported_numbers(generated: str, source_text: str) -> list[str]:
    """Números del texto generado que no aparecen en la fuente."""
    available: set[str] = set()
    for tok in numbers_in(source_text):
        available |= _canonical(tok)
    missing = []
    for tok in sorted(numbers_in(generated)):
        if _ignorable(tok):
            continue
        if not (_canonical(tok) & available):
            missing.append(tok)
    return missing

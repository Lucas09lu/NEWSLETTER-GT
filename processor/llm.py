"""Cliente de Claude para la curación (salida estructurada con Pydantic).

- Modelo configurable (`CLAUDE_MODEL`, por defecto `claude-opus-5-5`).
- `output_config.effort = "high"`: es trabajo editorial sensible a la calidad.
- Fallback del lado del servidor (`fallbacks="default"`): si el modelo declina
  una petición por sus filtros de seguridad, la API la reintenta con otro modelo
  en la misma llamada. Para desactivarlo: `CLAUDE_FALLBACKS=off`.
- Cualquier error (red, cuota, rechazo) se propaga como `CurationError` y el
  curador cae al modo heurístico: el pipeline nunca se queda sin edición.
"""

from __future__ import annotations

import logging
import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from config.settings import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class CurationError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.has_claude:
            raise CurationError("CLAUDE_API_KEY no configurada")
        self.model = settings.claude_model
        self.client = anthropic.Anthropic(
            api_key=settings.claude_api_key.get_secret_value(),
            max_retries=3,
            timeout=600.0,
        )
        self.use_fallbacks = os.getenv("CLAUDE_FALLBACKS", "on").lower() != "off"

    def structured(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": "high"},
        )
        if self.use_fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            response = self.client.beta.messages.parse(**kwargs)
        except anthropic.RateLimitError as exc:
            raise CurationError(f"Límite de uso de la API de Claude: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise CurationError("CLAUDE_API_KEY inválida") from exc
        except anthropic.APIStatusError as exc:
            raise CurationError(f"Error de la API de Claude ({exc.status_code}): {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise CurationError(f"Sin conexión con la API de Claude: {exc}") from exc

        if response.stop_reason == "refusal":
            raise CurationError(f"Claude declinó la petición: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise CurationError("Respuesta truncada (max_tokens); reducir el número de ítems")
        if response.parsed_output is None:
            raise CurationError("Claude no devolvió una salida válida para el esquema")
        usage = response.usage
        log.info(
            "Claude %s · %s · in=%s out=%s tokens",
            response.model,
            schema.__name__,
            usage.input_tokens,
            usage.output_tokens,
        )
        return response.parsed_output

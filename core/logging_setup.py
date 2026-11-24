"""Logging dual: consola legible + archivo JSONL por ejecución (`logs/`)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("source_id", "source_name", "connector", "status", "items"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None, run_name: str = "run") -> Path | None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return None
    root.setLevel(level.upper())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s · %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    log_file = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{run_name}.jsonl"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        root.addHandler(fh)

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
    return log_file

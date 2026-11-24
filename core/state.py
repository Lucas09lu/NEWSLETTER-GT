"""Persistencia de estado en JSON versionado (`data/state/`).

Se expone como un pequeño repositorio clave→documento para que en V2 se pueda
reemplazar por SQLite/Postgres sin tocar a los consumidores.

Documentos usados:
- `feeds_discovered.json`: caché de descubrimiento RSS por fuente.
- `static_index_seen.json`: links ya vistos en páginas índice estáticas.
- `source_health.json`: último resultado por fuente/conector.
- `sent_urls.json`: (Fase 2) URLs ya publicadas en ediciones anteriores.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonStateStore:
    def __init__(self, directory: Path, read_only: bool = False) -> None:
        self.directory = directory
        self.read_only = read_only
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()  # los conectores corren en paralelo

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def load(self, name: str, default: Any = None) -> Any:
        if name in self._cache:
            return self._cache[name]
        path = self._path(name)
        data = default if default is not None else {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("Estado corrupto en %s; se ignora y se recrea", path)
        self._cache[name] = data
        return data

    def save(self, name: str, data: Any) -> None:
        with self._lock:
            self._cache[name] = data
            if self.read_only:
                return
            self._write(name, data)

    def _write(self, name: str, data: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        # Escritura atómica: evita JSON truncado si el job se cancela.
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, path)

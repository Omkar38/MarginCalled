"""Load .env into the process environment.

Every entry point needs the same behaviour, and the one that did not have it
was a real trap: narrate.py read ANTHROPIC_API_KEY from os.environ but never
loaded the file the key is meant to live in, so pasting the key where the
comments say to put it would have had no effect and the narrator would have
gone on silently using its template fallback.

Existing environment variables win, so an explicitly exported value is never
overwritten by the file.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env"]

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | str | None = None) -> list[str]:
    """Set any variable defined in .env that is not already set.

    Returns the names loaded, never the values.
    """
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key or not value:
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded

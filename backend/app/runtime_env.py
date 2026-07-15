"""
Apply UI-set API keys into the process environment *before* any crawler is imported.

Optional API keys are persisted to ``runtime_api_keys.json`` (gitignored) by the
Settings UI. ``main.py`` also restores them into ``os.environ`` — but that restore runs
*after* the ``from crawlers.… import …`` block near the top of ``main.py``. Several
crawlers cache their key at module load (e.g. ``_TOKEN = os.getenv("IPINFO_TOKEN", "")``),
so if the injection only happened later they would capture an empty value and a key set
via the UI would silently do nothing until the container restarted.

Importing this module first — before the crawler imports — closes that gap: the keys are
in ``os.environ`` by the time each crawler module executes its top-level ``os.getenv``.

The value guard matches ``main.py``: docker-compose injects empty strings for optional
keys (``SHODAN_API_KEY=""``), so we only fill a var whose current value is blank, never
overwrite one that compose or the real environment already provided.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

RUNTIME_KEYS_PATH = Path(__file__).parent / "runtime_api_keys.json"


def load_runtime_keys() -> dict:
    """Return the persisted key store, or {} if missing/unreadable."""
    try:
        if RUNTIME_KEYS_PATH.exists():
            return json.loads(RUNTIME_KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def apply_runtime_keys() -> list[str]:
    """Inject stored keys into os.environ where the var is currently blank.

    Returns the list of env var names that were filled (useful for logging/tests).
    """
    applied: list[str] = []
    for name, value in load_runtime_keys().items():
        if value and not os.environ.get(name, "").strip():
            os.environ[name] = value
            applied.append(name)
    return applied


# Applied on import — importing this module *is* the side effect.
apply_runtime_keys()

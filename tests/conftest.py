"""
Shared fixtures for the Fieldwork smoke suite.

These are *integration smoke tests*: they drive the running docker-compose stack over
HTTP rather than importing the apps (which would pull in neo4j/httpx/etc.). So the stack
must be up — `docker compose up -d` — before running them. If it isn't reachable, the whole
suite skips with a clear message instead of erroring.

Base URLs are env-overridable so the same tests work if ports are remapped:
    SHELL_API   (default http://localhost:8002)
    LEGACY_API  (default http://localhost:8000)
    SHELL_WEB   (default http://localhost:3001)
    LEGACY_WEB  (default http://localhost:3000)
"""
import os
import functools

import httpx
import pytest

SHELL_API  = os.environ.get("SHELL_API",  "http://localhost:8002")
LEGACY_API = os.environ.get("LEGACY_API", "http://localhost:8000")
SHELL_WEB  = os.environ.get("SHELL_WEB",  "http://localhost:3001")
LEGACY_WEB = os.environ.get("LEGACY_WEB", "http://localhost:3000")


@functools.lru_cache(maxsize=None)
def _reachable(url: str) -> bool:
    try:
        # Any HTTP response (even 404) means the server is up.
        httpx.get(url, timeout=4.0)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_stack():
    """Skip the whole suite (clearly) if the backends aren't running."""
    if not (_reachable(SHELL_API + "/docs") or _reachable(LEGACY_API + "/docs")):
        pytest.skip(
            "Fieldwork stack not reachable on "
            f"{SHELL_API} / {LEGACY_API} — run `docker compose up -d` first.",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def client():
    """A shared HTTP client with a sane timeout (LLM routes get their own longer timeout)."""
    with httpx.Client(timeout=20.0, follow_redirects=True) as c:
        yield c


def get_json(client, base, path, **kwargs):
    """GET base+path, assert 2xx, return parsed JSON — with a helpful failure message."""
    r = client.get(base + path, **kwargs)
    assert r.status_code == 200, f"GET {path} → {r.status_code}: {r.text[:200]}"
    return r.json()

"""Stack is up and the module registry is intact — the cheapest regression net."""
import httpx
import pytest

from conftest import SHELL_API, LEGACY_API, SHELL_WEB, LEGACY_WEB, get_json

EXPECTED_MODULES = {"fieldwork", "news", "markets", "agent", "gigs", "presence", "reports"}


def test_shell_frontend_serves(client):
    r = client.get(SHELL_WEB + "/")
    assert r.status_code == 200
    assert "<" in r.text  # some HTML came back


def test_legacy_frontend_serves(client):
    r = client.get(LEGACY_WEB + "/")
    assert r.status_code == 200


def test_legacy_api_docs(client):
    assert client.get(LEGACY_API + "/docs").status_code == 200


def test_shell_module_registry(client):
    d = get_json(client, SHELL_API, "/api/shell/modules")
    mods = d["modules"] if isinstance(d, dict) else d   # {modules:[...], count}
    ids = {m["id"] for m in mods}
    missing = EXPECTED_MODULES - ids
    assert not missing, f"module registry missing {missing}; got {sorted(ids)}"


def test_shell_health(client):
    # /api/shell/health is a documented shell route; tolerate absence but not a 5xx.
    r = client.get(SHELL_API + "/api/shell/health")
    assert r.status_code in (200, 404), f"unexpected {r.status_code}"

"""
End-to-end AI pipeline smoke — slow (LLM calls + multi-tool fan-out) and network-heavy,
so excluded from the default run. Opt in with:  pytest -m slow

These prove the synthesis chain works at all (whichever engine is active). They do NOT
require Claude — if only Ollama is configured they still pass, just slower/weaker.
"""
import pytest

from conftest import SHELL_API, LEGACY_API

pytestmark = [pytest.mark.slow, pytest.mark.network]


def test_news_brief_generates(client):
    r = client.get(SHELL_API + "/api/news/brief", params={"window": 12}, timeout=120.0)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("engine") in {"claude", "claude-code", "ollama"}
    brief = d.get("brief") or d.get("text") or ""
    assert len(brief) > 50, "brief looks empty"


def test_orchestrate_investigation(client):
    r = client.post(
        LEGACY_API + "/investigate/orchestrate",
        json={"target": "tesla.com"},
        timeout=200.0,
    )
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("type") == "domain"
    assert d.get("tools_run"), "no tools ran"
    assert d.get("engine") in {"claude", "claude-code", "ollama"}
    assert len(d.get("brief", "")) > 50

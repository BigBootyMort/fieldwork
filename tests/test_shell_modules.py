"""Shell module endpoints return the expected shapes (data-agnostic)."""
import pytest

from conftest import SHELL_API, get_json


# ── News ──────────────────────────────────────────────────────────────────────

def test_news_llm_status(client):
    d = get_json(client, SHELL_API, "/api/news/llm-status")
    assert d["engine"] in {"claude", "claude-code", "ollama"}, d
    assert isinstance(d.get("ollama_ok"), bool)


def test_news_sources(client):
    d = get_json(client, SHELL_API, "/api/news/sources")
    # Response is a list of feeds or an object wrapping one — accept either.
    sources = d if isinstance(d, list) else d.get("sources", [])
    assert len(sources) > 0, "expected at least one RSS source configured"


def test_news_heatmap_shape(client):
    d = get_json(client, SHELL_API, "/api/news/heatmap", params={"window": 12})
    points = d.get("points", [])
    assert isinstance(points, list)
    if points:  # only assert shape when there's data (fresh stack may be empty)
        p = points[0]
        assert {"iso", "article_count"} <= p.keys(), p


def test_news_articles_shape(client):
    d = get_json(client, SHELL_API, "/api/news/articles", params={"window": 12})
    arts = d.get("articles", [])
    assert isinstance(arts, list)
    if arts:
        a = arts[0]
        assert {"id", "title", "url"} <= a.keys(), a.keys()


# ── Markets (hits Yahoo server-side → network-dependent) ──────────────────────

@pytest.mark.network
def test_markets_indicators(client):
    r = client.get(SHELL_API + "/api/markets/indicators", params={"symbol": "AAPL"})
    if r.status_code >= 500:
        pytest.skip(f"markets upstream unavailable ({r.status_code})")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert "ratios" in d, d.keys()
    score = d.get("tech_score")
    assert score is None or 0 <= score <= 100
    if d.get("verdict"):
        assert d["verdict"] in {"BUY", "HOLD", "SELL"}

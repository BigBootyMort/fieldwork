"""
VoidAccess dark-web threat-intel — https://github.com/KatrielMoses/voidaccess

Runs as a sibling container (docker-compose `voidaccess:` service) in lightweight CLI mode.
We reach its wrapper at VOIDACCESS_URL (default http://voidaccess:7004) and POST a free-text
query; it runs `voidaccess investigate` over Tor and exports the results as JSON.

Investigations are slow (a multi-step dark-web sweep), so we use shallow depth and a long
HTTP timeout, degrading to a reason string on timeout.

Complements ahmia/torbot: those find/crawl specific onions; VoidAccess runs a themed
threat-intel investigation (entities, sources, indicators) around a query/actor/campaign.
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.voidaccess")


def _base_url() -> str:
    return os.getenv("VOIDACCESS_URL", "http://voidaccess:7004").rstrip("/")


def _summarize(data) -> dict:
    """Counts from VoidAccess's report JSON (shape confirmed against a live run):
    entities, relationships, pages_scraped, communities + how many sources returned hits."""
    if not isinstance(data, dict):
        return {}
    sources = data.get("sources_used") or {}
    sources_hit = sum(1 for s in sources.values()
                      if isinstance(s, dict) and s.get("count", 0)) if isinstance(sources, dict) else 0
    return {
        "entities":      len(data.get("entities") or []),
        "relationships": len(data.get("relationships") or []),
        "pages_scraped": len(data.get("pages_scraped") or []),
        "communities":   data.get("community_count") or len(data.get("communities") or {}),
        "sources_hit":   sources_hit,
    }


def _top_entities(data, limit: int = 15) -> list:
    """Highest-confidence entities as compact {value, type, confidence} rows for the UI."""
    if not isinstance(data, dict):
        return []
    ents = [e for e in (data.get("entities") or []) if isinstance(e, dict)]
    ents.sort(key=lambda e: e.get("confidence") or 0, reverse=True)
    return [{
        "value":      str(e.get("canonical_value") or e.get("value") or "")[:200],
        "type":       e.get("entity_type") or "",
        "confidence": e.get("confidence"),
    } for e in ents[:limit]]


async def crawl_voidaccess(query: str, depth: str = "shallow", use_tor: bool = True) -> dict:
    """Run a VoidAccess threat-intel investigation. Returns the found/reason dict shape."""
    query = (query or "").strip()
    if len(query) < 2:
        return {"query": query, "found": False, "reason": "Query too short"}
    if depth not in ("shallow", "normal", "deep"):
        depth = "shallow"

    try:
        # Exceed the wrapper's own investigate cap (300s) plus export + overhead.
        async with httpx.AsyncClient(timeout=340.0) as client:
            r = await client.post(f"{_base_url()}/search",
                                  json={"query": query, "depth": depth,
                                        "use_tor": use_tor, "use_llm": False})
            if r.status_code == 504:
                return {"query": query, "found": False,
                        "reason": "VoidAccess timed out — try shallow depth"}
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        log.warning("VoidAccess HTTP error for %r: %s", query, exc)
        return {"query": query, "found": False, "reason": f"VoidAccess HTTP {exc.response.status_code}"}
    except Exception as exc:
        log.warning("VoidAccess failed for %r: %s", query, exc)
        return {"query": query, "found": False, "reason": str(exc)}

    if not data.get("found"):
        return {"query": query, "found": False,
                "reason": data.get("reason", "No results")}

    report = data.get("data")
    return {
        "query":             query,
        "found":             True,
        "depth":             depth,
        "investigation_id":  data.get("investigation_id", ""),
        "counts":            _summarize(report),
        "top_entities":      _top_entities(report),
    }

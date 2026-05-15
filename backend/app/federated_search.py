"""
Federated search — fires multiple OSINT sources in parallel and merges results.

Each source runs in its own asyncio task with an individual timeout.
Slow or unavailable sources are gracefully skipped — they never block the
response.  Results are labelled by source so the UI can group them.

Sources (all existing crawlers reused):
  internal   — Neo4j full-text index (always available)
  shodan     — IP/host intelligence        (requires SHODAN_API_KEY)
  virustotal — Domain/IP reputation        (requires VIRUSTOTAL_API_KEY)
  hibp       — Email breach lookup         (requires HIBP_API_KEY)
  github     — User / repo search          (requires GITHUB_TOKEN)
  news       — Recent press mentions       (requires NEWS_API_KEY)
  aleph      — OCCRP leaked docs & courts  (optional ALEPH_API_KEY)
  ahmia      — Dark-web mentions           (always, Tor not required)
  censys     — Internet scan data          (requires CENSYS_API_ID/SECRET)
  dehashed   — Credential breach search    (requires DEHASHED_KEY)
"""
import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("fieldwork.federated")

_SOURCE_TIMEOUT = 12.0   # seconds per source


# ── Internal graph source ─────────────────────────────────────────────────────

async def _search_internal(graph_db, query: str) -> list[dict]:
    from search import full_text_search
    results = await full_text_search(graph_db, query, limit=10)
    return [
        {
            "source":  "internal",
            "title":   r["title"],
            "snippet": r.get("snippet"),
            "url":     None,
            "meta":    {"label": r["label"], "id": r["id"], "score": r["score"]},
        }
        for r in results
    ]


# ── Shodan ────────────────────────────────────────────────────────────────────

async def _search_shodan(query: str) -> list[dict]:
    key = os.getenv("SHODAN_API_KEY", "")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://api.shodan.io/shodan/host/search",
                params={"key": key, "query": query, "minify": True, "page": 1},
            )
            r.raise_for_status()
            hits = r.json().get("matches", [])[:8]
    except Exception as exc:
        log.debug("Shodan federated: %s", exc)
        return []

    out = []
    for h in hits:
        ip = h.get("ip_str", "")
        out.append({
            "source":  "shodan",
            "title":   f"{ip}  {h.get('org', '')}".strip(),
            "snippet": f"Ports: {h.get('port')}  |  {h.get('data', '')[:120]}",
            "url":     f"https://www.shodan.io/host/{ip}",
            "meta":    {"ip": ip, "country": h.get("location", {}).get("country_name")},
        })
    return out


# ── VirusTotal ────────────────────────────────────────────────────────────────

async def _search_virustotal(query: str) -> list[dict]:
    key = os.getenv("VIRUSTOTAL_API_KEY", "")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://www.virustotal.com/api/v3/search",
                params={"query": query, "limit": 5},
                headers={"x-apikey": key},
            )
            r.raise_for_status()
            hits = r.json().get("data", [])
    except Exception as exc:
        log.debug("VT federated: %s", exc)
        return []

    out = []
    for h in hits:
        attrs = h.get("attributes", {})
        name  = attrs.get("name") or attrs.get("id") or h.get("id", "")
        stats = attrs.get("last_analysis_stats", {})
        mal   = stats.get("malicious", 0)
        out.append({
            "source":  "virustotal",
            "title":   name,
            "snippet": f"Malicious detections: {mal}" if mal else "No malicious detections",
            "url":     f"https://www.virustotal.com/gui/{h.get('type','file')}/{h.get('id','')}",
            "meta":    {"type": h.get("type"), "malicious": mal},
        })
    return out


# ── HIBP ──────────────────────────────────────────────────────────────────────

async def _search_hibp(query: str) -> list[dict]:
    key = os.getenv("HIBP_API_KEY", "")
    if not key or "@" not in query:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{query}",
                params={"truncateResponse": "false"},
                headers={"hibp-api-key": key, "user-agent": "Fieldwork-OSINT/1.0"},
            )
            if r.status_code == 404:
                return [{"source": "hibp", "title": f"{query} — no breaches found",
                         "snippet": None, "url": None, "meta": {}}]
            r.raise_for_status()
            breaches = r.json()
    except Exception as exc:
        log.debug("HIBP federated: %s", exc)
        return []

    return [
        {
            "source":  "hibp",
            "title":   f"{b['Name']} breach ({b.get('BreachDate','?')})",
            "snippet": f"{b.get('PwnCount', 0):,} accounts · {', '.join(b.get('DataClasses',[])[:4])}",
            "url":     f"https://haveibeenpwned.com/PwnedWebsites#{b['Name']}",
            "meta":    {"breach": b["Name"], "date": b.get("BreachDate")},
        }
        for b in breaches[:8]
    ]


# ── GitHub ────────────────────────────────────────────────────────────────────

async def _search_github(query: str) -> list[dict]:
    token = os.getenv("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://api.github.com/search/users",
                params={"q": query, "per_page": 5},
                headers=headers,
            )
            r.raise_for_status()
            users = r.json().get("items", [])
    except Exception as exc:
        log.debug("GitHub federated: %s", exc)
        return []

    return [
        {
            "source":  "github",
            "title":   u.get("login", ""),
            "snippet": f"GitHub {u.get('type','User')} · {u.get('html_url','')}",
            "url":     u.get("html_url"),
            "meta":    {"login": u.get("login"), "type": u.get("type")},
        }
        for u in users
    ]


# ── NewsAPI ───────────────────────────────────────────────────────────────────

async def _search_news(query: str) -> list[dict]:
    key = os.getenv("NEWS_API_KEY", "")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://newsapi.org/v2/everything",
                params={"q": query, "pageSize": 5, "sortBy": "relevancy", "apiKey": key},
            )
            r.raise_for_status()
            articles = r.json().get("articles", [])
    except Exception as exc:
        log.debug("News federated: %s", exc)
        return []

    return [
        {
            "source":  "news",
            "title":   a.get("title", ""),
            "snippet": a.get("description") or a.get("content", "")[:150],
            "url":     a.get("url"),
            "meta":    {"source": a.get("source", {}).get("name"), "published": a.get("publishedAt")},
        }
        for a in articles[:5]
    ]


# ── Aleph (OCCRP) ─────────────────────────────────────────────────────────────

async def _search_aleph(query: str) -> list[dict]:
    key  = os.getenv("ALEPH_API_KEY", "")
    base = "https://aleph.occrp.org/api/2"
    headers = {"Authorization": f"ApiKey {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                f"{base}/entities",
                params={"q": query, "limit": 5},
                headers=headers,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception as exc:
        log.debug("Aleph federated: %s", exc)
        return []

    out = []
    for ent in results:
        props = ent.get("properties", {})
        name  = (props.get("name") or [ent.get("id", "")])[0]
        out.append({
            "source":  "aleph",
            "title":   name,
            "snippet": f"{ent.get('schema','Entity')} · {ent.get('collection',{}).get('label','')}",
            "url":     f"https://aleph.occrp.org/entities/{ent.get('id','')}",
            "meta":    {"schema": ent.get("schema"), "collection": ent.get("collection",{}).get("label")},
        })
    return out


# ── Ahmia (dark web) ─────────────────────────────────────────────────────────

async def _search_ahmia(query: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://ahmia.fi/search/",
                params={"q": query},
                headers={"User-Agent": "Fieldwork-OSINT/1.0"},
            )
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select("li.result")[:5]
    except Exception as exc:
        log.debug("Ahmia federated: %s", exc)
        return []

    out = []
    for item in items:
        title_el   = item.select_one("a")
        snippet_el = item.select_one("p")
        out.append({
            "source":  "ahmia",
            "title":   title_el.get_text(strip=True) if title_el else "Dark web result",
            "snippet": snippet_el.get_text(strip=True)[:200] if snippet_el else None,
            "url":     title_el.get("href") if title_el else None,
            "meta":    {"onion": True},
        })
    return out


# ── Censys ────────────────────────────────────────────────────────────────────

async def _search_censys(query: str) -> list[dict]:
    api_id     = os.getenv("CENSYS_API_ID", "")
    api_secret = os.getenv("CENSYS_API_SECRET", "")
    if not api_id or not api_secret:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://search.censys.io/api/v2/hosts/search",
                params={"q": query, "per_page": 5},
                auth=(api_id, api_secret),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            hits = r.json().get("result", {}).get("hits", [])
    except Exception as exc:
        log.debug("Censys federated: %s", exc)
        return []

    out = []
    for h in hits:
        ip   = h.get("ip", "")
        svcs = [s.get("port") for s in h.get("services", []) if s.get("port")][:6]
        out.append({
            "source":  "censys",
            "title":   f"{ip}  {h.get('autonomous_system', {}).get('name', '')}".strip(),
            "snippet": f"Open ports: {', '.join(str(p) for p in svcs)}" if svcs else "No services listed",
            "url":     f"https://search.censys.io/hosts/{ip}",
            "meta":    {"ip": ip, "ports": svcs, "asn": h.get("autonomous_system", {}).get("asn")},
        })
    return out


# ── Dehashed ─────────────────────────────────────────────────────────────────

async def _search_dehashed(query: str) -> list[dict]:
    email = os.getenv("DEHASHED_EMAIL", "")
    key   = os.getenv("DEHASHED_KEY", "")
    if not email or not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_SOURCE_TIMEOUT) as c:
            r = await c.get(
                "https://api.dehashed.com/search",
                params={"query": query, "size": 5},
                auth=(email, key),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            entries = r.json().get("entries", []) or []
    except Exception as exc:
        log.debug("Dehashed federated: %s", exc)
        return []

    out = []
    for e in entries[:8]:
        parts = []
        if e.get("email"):    parts.append(f"Email: {e['email']}")
        if e.get("username"): parts.append(f"User: {e['username']}")
        if e.get("database_name"): parts.append(f"DB: {e['database_name']}")
        out.append({
            "source":  "dehashed",
            "title":   e.get("email") or e.get("username") or e.get("id", "Record"),
            "snippet": "  ·  ".join(parts),
            "url":     "https://www.dehashed.com",
            "meta":    {
                "email":    e.get("email"),
                "username": e.get("username"),
                "database": e.get("database_name"),
                "password": "redacted" if e.get("password") else None,
            },
        })
    return out


# ── Orchestrator ──────────────────────────────────────────────────────────────

_ALL_SOURCES = {
    "internal":   lambda q, g: _search_internal(g, q),
    "shodan":     lambda q, g: _search_shodan(q),
    "virustotal": lambda q, g: _search_virustotal(q),
    "hibp":       lambda q, g: _search_hibp(q),
    "github":     lambda q, g: _search_github(q),
    "news":       lambda q, g: _search_news(q),
    "aleph":      lambda q, g: _search_aleph(q),
    "ahmia":      lambda q, g: _search_ahmia(q),
    "censys":     lambda q, g: _search_censys(q),
    "dehashed":   lambda q, g: _search_dehashed(q),
}

SOURCE_ORDER = ["internal", "shodan", "virustotal", "hibp", "github",
                "news", "aleph", "ahmia", "censys", "dehashed"]


async def federated_search(
    graph_db,
    query: str,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run `query` against all requested sources in parallel.

    Returns:
        {
            "query": str,
            "results": { source_name: [result, ...] },
            "timings": { source_name: seconds },
            "errors":  { source_name: error_message },
            "total":   int,
        }
    """
    if sources is None:
        sources = SOURCE_ORDER
    else:
        sources = [s for s in sources if s in _ALL_SOURCES]

    import time

    async def run_source(name: str):
        fn = _ALL_SOURCES[name]
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(fn(query, graph_db), timeout=_SOURCE_TIMEOUT)
            return name, result, time.monotonic() - t0, None
        except asyncio.TimeoutError:
            return name, [], time.monotonic() - t0, "timeout"
        except Exception as exc:
            return name, [], time.monotonic() - t0, str(exc)

    raw = await asyncio.gather(*[run_source(s) for s in sources])

    results, timings, errors = {}, {}, {}
    total = 0
    for name, hits, elapsed, err in raw:
        results[name] = hits
        timings[name] = round(elapsed, 2)
        total += len(hits)
        if err:
            errors[name] = err

    return {
        "query":   query,
        "results": results,
        "timings": timings,
        "errors":  errors,
        "total":   total,
    }

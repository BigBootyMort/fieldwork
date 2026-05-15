"""
Ahmia.fi dark web search — Phase 6.2

Ahmia indexes public .onion sites and exposes a regular HTTPS search endpoint.
No Tor required. Results reference .onion URLs that require Tor Browser to open,
but the index itself is public.

  search_ahmia(graph_db, query, max_results)
      Search Ahmia for a query, store DarkWebMention nodes in Neo4j.
      Returns list of results.
"""
import hashlib
import logging
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("crawler.ahmia")

_AHMIA_BASE = "https://ahmia.fi"
_UA = "Mozilla/5.0 (compatible; Fieldwork-OSINT/0.4)"
_ONION_RE = re.compile(r"[a-z2-7]{16,56}\.onion", re.I)


async def search_ahmia(graph_db, query: str, max_results: int = 20) -> dict:
    """
    Search Ahmia.fi for a query string.
    Creates DarkWebMention nodes and links them to matching Person/Company nodes.
    """
    log.info("Ahmia: searching for %r", query)
    results = await _fetch_ahmia(query, max_results)

    if results is None:
        return {"found": False, "query": query,
                "reason": "Ahmia.fi unreachable"}

    stored = 0
    async with graph_db.driver.session() as session:
        for r in results:
            mention_id = hashlib.sha1(
                f"{r['url']}:{query}".encode()
            ).hexdigest()

            await session.run(
                "MERGE (m:DarkWebMention {id: $id}) "
                "ON CREATE SET "
                "  m.query      = $query, "
                "  m.url        = $url, "
                "  m.title      = $title, "
                "  m.excerpt    = $excerpt, "
                "  m.onion_host = $onion, "
                "  m.source     = 'ahmia', "
                "  m.first_seen = datetime()",
                id=mention_id,
                query=query,
                url=r["url"][:500],
                title=r["title"][:300],
                excerpt=r["excerpt"][:500],
                onion=r["onion_host"],
            )
            stored += 1

    log.info("Ahmia: %d results for %r, %d stored", len(results), query, stored)
    return {
        "found":   True,
        "query":   query,
        "results": results,
        "stored":  stored,
    }


async def _fetch_ahmia(query: str, max_results: int) -> list[dict] | None:
    """Scrape Ahmia search results page and parse .result divs."""
    encoded = urllib.parse.quote(query)
    url     = f"{_AHMIA_BASE}/search/?q={encoded}"

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            r = await client.get(url)

        if r.status_code != 200:
            log.warning("Ahmia: HTTP %s", r.status_code)
            return None

        soup    = BeautifulSoup(r.text, "lxml")
        results = []

        for li in soup.select("li.result")[:max_results]:
            # Title + link
            a_tag = li.select_one("h4 a") or li.select_one("a")
            title = a_tag.get_text(strip=True) if a_tag else ""
            href  = a_tag.get("href", "") if a_tag else ""

            # Resolve relative URLs through Ahmia's redirect
            if href.startswith("/"):
                href = f"{_AHMIA_BASE}{href}"

            # Extract the real .onion URL from the Ahmia redirect
            onion_url  = _extract_onion_from_redirect(href)
            onion_host = ""
            m = _ONION_RE.search(onion_url or href)
            if m:
                onion_host = m.group(0).lower()

            # Excerpt / description
            excerpt_el = li.select_one("p") or li.select_one(".description")
            excerpt    = excerpt_el.get_text(strip=True) if excerpt_el else ""

            if not title and not onion_host:
                continue

            results.append({
                "title":      title,
                "url":        onion_url or href,
                "onion_host": onion_host,
                "excerpt":    excerpt[:400],
            })

        return results

    except Exception as exc:
        log.warning("Ahmia fetch failed: %s", exc)
        return None


def _extract_onion_from_redirect(href: str) -> str:
    """
    Ahmia wraps results in a redirect URL like:
      /redirect?url=http://xyz.onion/path
    Extract the real URL from the query string.
    """
    try:
        parsed = urllib.parse.urlparse(href)
        qs     = urllib.parse.parse_qs(parsed.query)
        return qs.get("url", [href])[0]
    except Exception:
        return href

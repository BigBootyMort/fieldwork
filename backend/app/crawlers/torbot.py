"""
TorBot dark-web crawler — https://github.com/DedSecInside/TorBot

Runs as a sibling container (docker-compose `torbot:` service) that bundles its own Tor
daemon. We reach its wrapper at TORBOT_URL (default http://torbot:7003) and POST a URL +
depth; it crawls over Tor and returns the discovered link tree. Crawling .onion sites over
Tor is slow, so keep depth small (1–2).

Complements the existing `ahmia` crawler: Ahmia *searches* a clearnet index of .onion
sites; TorBot *crawls* a specific .onion (or clearnet) URL to map its outbound links.
"""
import logging
import os
import re

import httpx

log = logging.getLogger("fieldwork.torbot")

_URL_RE = re.compile(r"^https?://", re.I)


def _base_url() -> str:
    return os.getenv("TORBOT_URL", "http://torbot:7003").rstrip("/")


async def crawl_onion_torbot(url: str, depth: int = 1) -> dict:
    """Crawl a URL (usually a .onion) via the TorBot service. Returns the found/reason shape."""
    url = (url or "").strip()
    if not _URL_RE.match(url):
        return {"url": url, "found": False, "reason": "URL must start with http:// or https://"}
    depth = max(1, min(int(depth or 1), 3))

    try:
        # Timeout must exceed the service's own crawl cap (~170s) plus overhead.
        async with httpx.AsyncClient(timeout=185.0) as client:
            r = await client.post(f"{_base_url()}/search", json={"url": url, "depth": depth})
            if r.status_code == 504:
                return {"url": url, "found": False, "depth": depth,
                        "reason": "TorBot timed out — try a shallower depth"}
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        log.warning("TorBot HTTP error for %s: %s", url, exc)
        return {"url": url, "found": False, "reason": f"TorBot HTTP {exc.response.status_code}"}
    except Exception as exc:
        log.warning("TorBot crawl failed for %s: %s", url, exc)
        return {"url": url, "found": False, "reason": str(exc)}

    count = data.get("count", 0)
    nodes = data.get("nodes", []) or []
    if not count and not nodes:
        return {"url": url, "found": False, "depth": depth,
                "reason": data.get("reason", "No links discovered")}

    onion = [n for n in nodes if ".onion" in n]
    return {
        "url":         url,
        "found":       True,
        "depth":       depth,
        "title":       data.get("title", ""),
        "count":       count,
        "onion_count": len(onion),
        "nodes":       nodes[:100],
    }

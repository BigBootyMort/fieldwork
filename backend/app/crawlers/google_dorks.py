"""
Google Custom Search Engine (CSE) dork runner.
https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list

Requires GOOGLE_CSE_KEY and GOOGLE_CSE_CX env vars.
Both are available on a free Google account (100 queries/day free).
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.google_dorks")

_KEY = os.getenv("GOOGLE_CSE_KEY", "")
_CX  = os.getenv("GOOGLE_CSE_CX",  "")
_URL = "https://www.googleapis.com/customsearch/v1"


async def run_dork(query: str, num: int = 10) -> dict:
    """
    Execute a Google CSE search query and return structured results.
    Requires GOOGLE_CSE_KEY and GOOGLE_CSE_CX to be configured.
    """
    if not _KEY or not _CX:
        return {
            "query":  query,
            "found":  False,
            "reason": "Not configured — set GOOGLE_CSE_KEY and GOOGLE_CSE_CX",
        }

    num = max(1, min(num, 10))  # CSE allows 1–10 per request

    params = {
        "key": _KEY,
        "cx":  _CX,
        "q":   query,
        "num": num,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Fieldwork OSINT"}) as client:
            r = await client.get(_URL, params=params)
            if r.status_code == 400:
                return {"query": query, "found": False, "reason": "Bad request — check your CSE configuration"}
            if r.status_code == 403:
                return {"query": query, "found": False, "reason": "Invalid API key or quota exceeded"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("Google Dork search failed: %s", exc)
        return {"query": query, "found": False, "reason": str(exc)}

    items = data.get("items", [])
    results = [
        {
            "title":   item.get("title", ""),
            "link":    item.get("link",  ""),
            "snippet": item.get("snippet", ""),
        }
        for item in items
    ]

    total = int(data.get("searchInformation", {}).get("totalResults", 0))

    return {
        "query":         query,
        "found":         bool(results),
        "results":       results,
        "total_results": total,
    }

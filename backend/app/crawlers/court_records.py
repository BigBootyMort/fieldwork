"""US court record search via CourtListener (free, no API key required).

Uses the /search/?type=o (opinions) endpoint which is publicly accessible
without authentication, unlike /search/?type=p (people/parties) which
requires a login since CL's 2024 API changes.
"""
import logging
import httpx

log = logging.getLogger("fieldwork.court_records")
COURTLISTENER_URL = "https://www.courtlistener.com/api/rest/v4"
_BASE = "https://www.courtlistener.com"
_HEADERS = {"User-Agent": "Fieldwork OSINT research@fieldwork.local"}


async def search_court_records(name: str) -> dict:
    """Search CourtListener for US federal and state court opinions by name."""
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(
                f"{COURTLISTENER_URL}/search/",
                params={
                    "q": f'"{name}"',
                    "type": "o",          # opinions — public, no auth required
                    "order_by": "score desc",
                    "format": "json",
                },
            )

        if resp.status_code == 401:
            return {"name": name, "found": False, "reason": "Authentication required by CourtListener"}
        if resp.status_code != 200:
            return {"name": name, "found": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        raw_results = data.get("results", [])
        cases = []
        for r in raw_results[:15]:  # cap at 15
            abs_url = r.get("absolute_url", "")
            full_url = f"{_BASE}{abs_url}" if abs_url.startswith("/") else abs_url
            cases.append({
                "case_name":     r.get("caseName") or r.get("case_name", ""),
                "court":         r.get("court_id") or r.get("court", ""),
                "date_filed":    r.get("dateFiled") or r.get("date_filed", ""),
                "status":        r.get("status", ""),
                "url":           full_url,
                "docket_number": r.get("docketNumber") or r.get("docket_number", ""),
                "snippet":       r.get("snippet", ""),
            })

        return {
            "name":  name,
            "found": bool(cases),
            "cases": cases,
            "total": data.get("count", len(cases)),
        }
    except Exception as exc:
        log.warning("CourtListener error for %r: %s", name, exc)
        return {"name": name, "found": False, "error": str(exc)}

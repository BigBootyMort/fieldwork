"""
Wayback Machine (Internet Archive) enrichment.

Uses the CDX API to query historical crawl records for a domain:
  - First and last archived dates
  - Total snapshot count
  - Notable paths (staff pages, contact pages, about pages)

On-demand via GET /enrich/domain/{domain}/wayback.

No API key required. The CDX API is a public read endpoint.
"""
import httpx
import logging
import re
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("wayback")

CDX_URL = "https://web.archive.org/cdx/search/cdx"
AVAILABLE_URL = "https://archive.org/wayback/available"

# Paths that often contain useful information (staff listings, contact details)
_INTERESTING_RE = re.compile(
    r"/(staff|team|people|person|about|contact|board|leadership|executive|director|investor)",
    re.IGNORECASE,
)

_limiter = AsyncLimiter(max_rate=2, time_period=1)


async def enrich_domain_wayback(graph_db, domain: str) -> dict:
    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": "fieldwork-osint/0.3"}, follow_redirects=True
    ) as client:
        # Summary: collapse by year to get first/last/count without huge payloads
        async with _limiter:
            summary_resp = await client.get(CDX_URL, params={
                "url": f"{domain}/*",
                "output": "json",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:200",
                "collapse": "timestamp:6",   # one record per month
                "limit": "120",
            })

        # Closest available snapshot URL
        async with _limiter:
            avail_resp = await client.get(AVAILABLE_URL, params={"url": domain})

    result: dict = {"domain": domain, "found": False}

    # Parse availability
    if avail_resp.status_code == 200:
        avail = avail_resp.json()
        snapshot = (avail.get("archived_snapshots") or {}).get("closest") or {}
        if snapshot.get("available"):
            result["closest_snapshot"] = snapshot.get("url", "")
            result["found"] = True

    # Parse CDX records
    if summary_resp.status_code != 200:
        return result

    try:
        rows = summary_resp.json()
    except Exception:
        return result

    if not rows or len(rows) < 2:
        return result

    result["found"] = True
    # rows[0] is the header ["timestamp","original","statuscode"]
    records = rows[1:]
    timestamps = [r[0] for r in records if r and r[0]]
    urls       = [r[1] for r in records if len(r) > 1]

    if timestamps:
        first_ts = min(timestamps)
        last_ts  = max(timestamps)
        result["first_archived"] = f"{first_ts[:4]}-{first_ts[4:6]}-{first_ts[6:8]}"
        result["last_archived"]  = f"{last_ts[:4]}-{last_ts[4:6]}-{last_ts[6:8]}"
        result["snapshot_count"] = len(records)

    # Surface paths likely to contain person/org data
    interesting = sorted({
        u for u in urls if _INTERESTING_RE.search(u)
    })[:10]
    result["interesting_paths"] = interesting

    # Write to Neo4j
    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (d:Domain {id: $id}) "
            "ON CREATE SET d.name = $id, d.first_seen = datetime() "
            "SET d.wayback_first  = $first, "
            "    d.wayback_last   = $last, "
            "    d.wayback_count  = $count, "
            "    d.wayback_fetched = datetime()",
            id=domain,
            first=result.get("first_archived", ""),
            last=result.get("last_archived", ""),
            count=result.get("snapshot_count", 0),
        )

    log.info(
        "Wayback %s: first=%s last=%s snapshots=%s interesting=%d",
        domain,
        result.get("first_archived", "?"),
        result.get("last_archived", "?"),
        result.get("snapshot_count", 0),
        len(interesting),
    )
    return result

"""
URLScan.io — domain/URL scan history and result enrichment.

URLScan.io is a free service that scans URLs on submission and indexes results.
This module:
  1. Searches URLScan.io for existing scans of a domain (no API key required).
  2. Extracts: page titles, IPs resolved, technologies detected, verdicts,
     country, TLS info, and screenshot thumbnail URLs.
  3. Persists discovered IPs as IP nodes linked to the Domain via RESOLVES_TO.

No API key required for the search endpoint (rate-limited to ~100 req/min).
Submission of new scans requires an API key (URLSCAN_API_KEY in .env) but
this module only performs search + read operations by default.
"""
import os
import logging
import hashlib
import httpx

log = logging.getLogger("fieldwork.urlscan")

_SEARCH_URL  = "https://urlscan.io/api/v1/search/"
_RESULT_URL  = "https://urlscan.io/api/v1/result/{uuid}/"
_API_KEY     = os.getenv("URLSCAN_API_KEY", "")

_HEADERS = {
    "User-Agent": "Fieldwork OSINT / urlscan-lookup",
    **({"API-Key": _API_KEY} if _API_KEY else {}),
}


async def search_domain(graph_db, domain: str, limit: int = 10) -> dict:
    """
    Search URLScan.io for the most recent scans of `domain`.

    Returns a dict with:
      - domain          — the queried domain
      - scan_count      — total scans indexed by urlscan.io
      - scans           — list of up to `limit` scans, each containing:
          uuid, task_url, task_time, page_title, page_ip, page_country,
          page_server, screenshot_url, verdict_malicious, verdict_score,
          technologies (list of names), ips_seen (list)
    """
    domain = domain.strip().lower()
    result: dict = {
        "domain":      domain,
        "scan_count":  0,
        "scans":       [],
        "error":       None,
    }

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers=_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                _SEARCH_URL,
                params={"q": f"domain:{domain}", "size": min(limit, 100)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("urlscan.io search failed for %s: %s", domain, exc)
        result["error"] = str(exc)
        return result

    hits        = data.get("results", [])
    result["scan_count"] = data.get("total", len(hits))

    scans: list = []
    new_ips: set[str] = set()

    for hit in hits[:limit]:
        page  = hit.get("page",  {})
        task  = hit.get("task",  {})
        stats = hit.get("stats", {})
        verdicts = hit.get("verdicts", {}).get("overall", {})

        # Technologies from Wappalyzer integration
        techs: list[str] = []
        for t in (hit.get("page", {}).get("technologies") or []):
            name = t.get("name") or t.get("app") or ""
            if name: techs.append(name)

        # IPs observed during the scan
        ips_seen: list[str] = []
        for ip_entry in (hit.get("page", {}).get("ips") or []):
            if isinstance(ip_entry, str): ips_seen.append(ip_entry)

        ip = page.get("ip", "")
        if ip: ips_seen.append(ip)
        ips_seen = list(dict.fromkeys(ips_seen))  # deduplicate

        scan = {
            "uuid":              hit.get("_id", ""),
            "task_url":          task.get("url", ""),
            "task_time":         (task.get("time") or "")[:19],
            "page_title":        page.get("title", ""),
            "page_ip":           ip,
            "page_country":      page.get("country", ""),
            "page_server":       page.get("server", ""),
            "screenshot_url":    hit.get("screenshot", ""),
            "result_url":        f"https://urlscan.io/result/{hit.get('_id', '')}",
            "verdict_malicious": bool(verdicts.get("malicious", False)),
            "verdict_score":     int(verdicts.get("score", 0)),
            "technologies":      techs[:20],
            "ips_seen":          ips_seen[:10],
        }
        scans.append(scan)
        new_ips.update(ips_seen)

    result["scans"] = scans

    # ── Persist: create IP nodes linked to the domain ────────────────────────
    if new_ips:
        dom_id = f"domain_{hashlib.sha1(domain.encode()).hexdigest()[:12]}"
        async with graph_db.driver.session() as session:
            # Ensure domain node exists
            await session.run("""
                MERGE (d:Domain {id: $did})
                ON CREATE SET d.domain = $dom, d.name = $dom,
                              d.source = 'urlscan', d.created_at = datetime()
                ON MATCH  SET d.domain = $dom
            """, did=dom_id, dom=domain)

            for ip in new_ips:
                ip_id = f"ip_{hashlib.sha1(ip.encode()).hexdigest()[:12]}"
                await session.run("""
                    MERGE (i:IP {id: $iid})
                    ON CREATE SET i.address = $ip, i.source = 'urlscan', i.created_at = datetime()
                    ON MATCH  SET i.address = $ip
                    WITH i
                    MATCH (d:Domain {id: $did})
                    MERGE (d)-[r:RESOLVES_TO]->(i)
                    ON CREATE SET r.source = 'urlscan', r.first_seen = datetime()
                """, iid=ip_id, ip=ip, did=dom_id)

    log.info("urlscan.io: %d scans for %s, %d IPs persisted",
             len(scans), domain, len(new_ips))
    return result

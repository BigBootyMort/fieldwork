"""
Shodan IP enrichment.

Called on-demand via GET /enrich/ip/{ip} — deliberately not wired into
the automatic crawl pipeline because the free tier is 100 credits/month
and each host lookup costs 1 credit. Spend them on IPs that matter.

Enriches the IP node with org, ASN, open ports, tags, and vuln count.
Creates Location nodes for the host's city/country and a Company node
for the registered organisation.
"""
import httpx
import os
import logging

log = logging.getLogger("shodan")

_SHODAN_API = "https://api.shodan.io"


def _slug(text: str) -> str:
    import re, unicodedata
    n = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")[:180] or "unknown"


async def enrich_ip_shodan(graph_db, ip: str) -> dict:
    api_key = os.getenv("SHODAN_API_KEY", "")
    if not api_key:
        return {"ip": ip, "found": False, "reason": "no SHODAN_API_KEY configured"}

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": "fieldwork-osint/0.2"}
    ) as client:
        resp = await client.get(
            f"{_SHODAN_API}/shodan/host/{ip}",
            params={"key": api_key},
        )

    if resp.status_code == 404:
        return {"ip": ip, "found": False, "reason": "not in Shodan"}
    if resp.status_code == 401:
        return {"ip": ip, "found": False, "reason": "invalid Shodan API key"}
    if resp.status_code == 403:
        return {"ip": ip, "found": False, "reason": "Shodan quota exceeded"}
    if resp.status_code != 200:
        return {"ip": ip, "found": False, "reason": f"HTTP {resp.status_code}"}

    data = resp.json()

    ports = sorted(set(data.get("ports", [])))
    org = (data.get("org") or data.get("isp") or "").strip()
    asn = (data.get("asn") or "").strip()
    country = (data.get("country_name") or "").strip()
    city = (data.get("city") or "").strip()
    tags = data.get("tags", [])
    vulns = list((data.get("vulns") or {}).keys())

    result = {
        "ip": ip,
        "found": True,
        "org": org,
        "asn": asn,
        "country": country,
        "city": city,
        "ports": ports,
        "tags": tags,
        "vuln_count": len(vulns),
        "vulns": vulns[:20],
    }

    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (i:IP {id: $ip}) "
            "ON CREATE SET i.address = $ip, i.first_seen = datetime() "
            "SET i.org = $org, i.asn = $asn, i.ports = $ports, "
            "    i.tags = $tags, i.vuln_count = $vuln_count, "
            "    i.shodan_fetched = datetime()",
            ip=ip, org=org, asn=asn, ports=ports, tags=tags, vuln_count=len(vulns),
        )

        # Location — prefer city+country, fall back to country only
        if city and country:
            loc_name = f"{city}, {country}"
            await session.run(
                "MERGE (l:Location {id: $id}) "
                "ON CREATE SET l.name = $name, l.first_seen = datetime() "
                "WITH l MATCH (i:IP {id: $ip}) "
                "MERGE (i)-[r:LOCATED_AT]->(l) "
                "ON CREATE SET r.source = 'shodan', r.first_seen = datetime()",
                id=_slug(loc_name), name=loc_name, ip=ip,
            )
        elif country:
            await session.run(
                "MERGE (l:Location {id: $id}) "
                "ON CREATE SET l.name = $name, l.first_seen = datetime() "
                "WITH l MATCH (i:IP {id: $ip}) "
                "MERGE (i)-[r:IN_COUNTRY]->(l) "
                "ON CREATE SET r.source = 'shodan', r.first_seen = datetime()",
                id=_slug(country), name=country, ip=ip,
            )

        # Owning organisation → Company node + OWNS_IP edge
        if org and len(org) > 2:
            await session.run(
                "MERGE (c:Company {id: $id}) "
                "ON CREATE SET c.name = $name, c.created_at = datetime() "
                "WITH c MATCH (i:IP {id: $ip}) "
                "MERGE (c)-[r:OWNS_IP]->(i) "
                "ON CREATE SET r.source = 'shodan', r.first_seen = datetime()",
                id=_slug(org), name=org, ip=ip,
            )

    log.info("Shodan enriched %s: org=%r ports=%s vulns=%d", ip, org, ports, len(vulns))
    return result

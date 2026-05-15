"""
VirusTotal domain and IP reputation enrichment.

On-demand via:
  GET /enrich/domain/{domain}/vt  → detection stats + passive DNS → IP nodes
  GET /enrich/ip/{ip}/vt          → detection stats + reverse DNS → Domain nodes

Free tier: 4 requests/minute, 500/day. Both functions respect that by
being on-demand rather than wired into the automatic crawl pipeline.

Passive DNS is the most valuable output: it reveals historical A-record
mappings that link domains to infrastructure over time.
"""
import httpx
import os
import logging

log = logging.getLogger("virustotal")

_VT_API = "https://www.virustotal.com/api/v3"


def _headers() -> dict:
    return {
        "x-apikey": os.getenv("VIRUSTOTAL_API_KEY", ""),
        "User-Agent": "fieldwork-osint/0.2",
    }


async def enrich_domain_vt(graph_db, domain: str) -> dict:
    if not os.getenv("VIRUSTOTAL_API_KEY"):
        return {"domain": domain, "found": False, "reason": "no VIRUSTOTAL_API_KEY configured"}

    async with httpx.AsyncClient(timeout=20.0, headers=_headers()) as client:
        resp = await client.get(f"{_VT_API}/domains/{domain}")

    if resp.status_code == 404:
        return {"domain": domain, "found": False, "reason": "not in VirusTotal"}
    if resp.status_code in (401, 403):
        return {"domain": domain, "found": False, "reason": "invalid VirusTotal API key"}
    if resp.status_code == 429:
        return {"domain": domain, "found": False, "reason": "VirusTotal rate limit — try again in 60s"}
    if resp.status_code != 200:
        return {"domain": domain, "found": False, "reason": f"HTTP {resp.status_code}"}

    attrs = ((resp.json().get("data") or {}).get("attributes")) or {}
    stats = attrs.get("last_analysis_stats") or {}
    malicious = stats.get("malicious", 0)
    suspicious = stats.get("suspicious", 0)
    categories = list((attrs.get("categories") or {}).values())

    # Passive DNS: A records from last_dns_records
    resolved_ips = [
        r["value"] for r in (attrs.get("last_dns_records") or [])
        if r.get("type") == "A" and r.get("value")
    ]

    result = {
        "domain": domain,
        "found": True,
        "malicious": malicious,
        "suspicious": suspicious,
        "categories": categories[:10],
        "resolved_ips": resolved_ips,
        "registrar": attrs.get("registrar", ""),
        "creation_date": attrs.get("creation_date"),
    }

    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (d:Domain {id: $id}) "
            "ON CREATE SET d.name = $id, d.first_seen = datetime() "
            "SET d.vt_malicious = $malicious, d.vt_suspicious = $suspicious, "
            "    d.vt_categories = $cats, d.vt_fetched = datetime()",
            id=domain, malicious=malicious, suspicious=suspicious, cats=categories[:10],
        )

        for ip in resolved_ips[:20]:
            await session.run(
                "MERGE (i:IP {id: $ip}) "
                "ON CREATE SET i.address = $ip, i.first_seen = datetime() "
                "WITH i MATCH (d:Domain {id: $did}) "
                "MERGE (d)-[r:RESOLVED_TO]->(i) "
                "ON CREATE SET r.source = 'virustotal', r.first_seen = datetime()",
                ip=ip, did=domain,
            )

    log.info(
        "VT domain %s: malicious=%d suspicious=%d ips=%d",
        domain, malicious, suspicious, len(resolved_ips),
    )
    return result


async def enrich_ip_vt(graph_db, ip: str) -> dict:
    if not os.getenv("VIRUSTOTAL_API_KEY"):
        return {"ip": ip, "found": False, "reason": "no VIRUSTOTAL_API_KEY configured"}

    async with httpx.AsyncClient(timeout=20.0, headers=_headers()) as client:
        resp = await client.get(f"{_VT_API}/ip_addresses/{ip}")

    if resp.status_code == 404:
        return {"ip": ip, "found": False, "reason": "not in VirusTotal"}
    if resp.status_code in (401, 403):
        return {"ip": ip, "found": False, "reason": "invalid VirusTotal API key"}
    if resp.status_code == 429:
        return {"ip": ip, "found": False, "reason": "VirusTotal rate limit — try again in 60s"}
    if resp.status_code != 200:
        return {"ip": ip, "found": False, "reason": f"HTTP {resp.status_code}"}

    attrs = ((resp.json().get("data") or {}).get("attributes")) or {}
    stats = attrs.get("last_analysis_stats") or {}
    malicious = stats.get("malicious", 0)
    asn = attrs.get("asn", "")
    org = (attrs.get("as_owner") or "").strip()
    country = (attrs.get("country") or "").strip()

    # Reverse DNS → Domain nodes
    rdns_domains = [
        r["value"].rstrip(".")
        for r in (attrs.get("last_dns_records") or [])
        if r.get("type") == "PTR" and r.get("value")
    ]

    result = {
        "ip": ip,
        "found": True,
        "malicious": malicious,
        "asn": asn,
        "org": org,
        "country": country,
        "reverse_dns": rdns_domains[:10],
    }

    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (i:IP {id: $ip}) "
            "ON CREATE SET i.address = $ip, i.first_seen = datetime() "
            "SET i.vt_malicious = $malicious, i.asn = $asn, "
            "    i.org = $org, i.vt_fetched = datetime()",
            ip=ip, malicious=malicious, asn=str(asn), org=org,
        )

        for domain in rdns_domains[:10]:
            await session.run(
                "MERGE (d:Domain {id: $did}) "
                "ON CREATE SET d.name = $did, d.first_seen = datetime() "
                "WITH d MATCH (i:IP {id: $ip}) "
                "MERGE (d)-[r:RESOLVED_TO]->(i) "
                "ON CREATE SET r.source = 'virustotal:rdns', r.first_seen = datetime()",
                did=domain, ip=ip,
            )

    log.info("VT IP %s: malicious=%d org=%r rdns=%d", ip, malicious, org, len(rdns_domains))
    return result

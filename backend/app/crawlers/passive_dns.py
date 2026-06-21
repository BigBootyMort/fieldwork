"""
Phase 32 — Passive DNS + Reverse IP lookup.

Free data sources (no API key required):
  • HackerTarget  — subdomain enumeration, reverse IP lookup
  • VirusTotal    — passive DNS resolution history (if key available)

Results are merged into the graph:
  • Subdomains    → Domain nodes connected to the parent domain
  • IP history    → IP nodes with RESOLVED_TO relationships
  • Co-hosted     → Domain nodes sharing the same IP
"""
import logging
import os
import re
from typing import Optional
import httpx

log = logging.getLogger("fieldwork.passive_dns")

_HACKERTARGET_TIMEOUT = 15
_VT_KEY = os.getenv("VIRUSTOTAL_API_KEY", "")

_IP_RE     = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9\-]+)+$", re.I)


# ── helpers ───────────────────────────────────────────────────────────────────

def _node_id(label: str, value: str) -> str:
    import hashlib
    return f"{label.lower()}_{hashlib.sha1(value.lower().encode()).hexdigest()[:12]}"


async def _merge_domain(session, domain: str, source: str = "passive_dns") -> str:
    nid = _node_id("Domain", domain)
    await session.run("""
        MERGE (d:Domain {id: $id})
        ON CREATE SET d.domain = $domain, d.name = $domain,
                      d.source = $source, d.created_at = datetime()
        ON MATCH  SET d.domain = $domain
    """, id=nid, domain=domain, source=source)
    return nid


async def _merge_ip(session, ip: str, source: str = "passive_dns") -> str:
    nid = _node_id("IP", ip)
    await session.run("""
        MERGE (n:IP {id: $id})
        ON CREATE SET n.ip = $ip, n.address = $ip,
                      n.source = $source, n.created_at = datetime()
        ON MATCH  SET n.ip = $ip
    """, id=nid, ip=ip, source=source)
    return nid


# ── HackerTarget — subdomain search ──────────────────────────────────────────

async def _ht_hostsearch(domain: str) -> list[dict]:
    """
    Returns list of {subdomain, ip} dicts via HackerTarget hostsearch.
    Rate-limited to ~100 req/day on free tier — fails gracefully.
    """
    url = f"https://api.hackertarget.com/hostsearch/?q={domain}"
    try:
        async with httpx.AsyncClient(timeout=_HACKERTARGET_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Fieldwork OSINT"})
            r.raise_for_status()
            text = r.text.strip()
            if "error check your" in text.lower() or "API count" in text:
                log.debug("HackerTarget rate limited for %s", domain)
                return []
            results = []
            for line in text.splitlines():
                parts = line.strip().split(",")
                if len(parts) == 2:
                    sub, ip = parts[0].strip(), parts[1].strip()
                    if _DOMAIN_RE.match(sub) and _IP_RE.match(ip):
                        results.append({"subdomain": sub, "ip": ip})
            return results
    except Exception as exc:
        log.debug("HackerTarget hostsearch failed for %s: %s", domain, exc)
        return []


async def _ht_reverse_ip(ip: str) -> list[str]:
    """Return list of domain names co-hosted on an IP."""
    url = f"https://api.hackertarget.com/reverseiplookup/?q={ip}"
    try:
        async with httpx.AsyncClient(timeout=_HACKERTARGET_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Fieldwork OSINT"})
            r.raise_for_status()
            text = r.text.strip()
            if "error" in text.lower() or "API count" in text:
                return []
            return [
                line.strip() for line in text.splitlines()
                if _DOMAIN_RE.match(line.strip())
            ]
    except Exception as exc:
        log.debug("HackerTarget reverseIP failed for %s: %s", ip, exc)
        return []


# ── VirusTotal — passive DNS ──────────────────────────────────────────────────

async def _vt_domain_resolutions(domain: str) -> list[dict]:
    """Return recent IP resolutions from VT passive DNS. Requires API key."""
    if not _VT_KEY:
        return []
    url = f"https://www.virustotal.com/api/v3/domains/{domain}/resolutions"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                url,
                headers={"x-apikey": _VT_KEY},
                params={"limit": 20},
            )
            r.raise_for_status()
            items = r.json().get("data", [])
            results = []
            for item in items:
                attrs = item.get("attributes", {})
                ip    = attrs.get("ip_address", "")
                date  = attrs.get("date", 0)
                if ip and _IP_RE.match(ip):
                    results.append({"ip": ip, "last_seen": date})
            return results
    except Exception as exc:
        log.debug("VT passive DNS failed for %s: %s", domain, exc)
        return []


async def _vt_ip_resolutions(ip: str) -> list[dict]:
    """Return domains that resolved to this IP from VT. Requires API key."""
    if not _VT_KEY:
        return []
    url = f"https://www.virustotal.com/api/v3/ip_addresses/{ip}/resolutions"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                url,
                headers={"x-apikey": _VT_KEY},
                params={"limit": 20},
            )
            r.raise_for_status()
            items = r.json().get("data", [])
            results = []
            for item in items:
                attrs  = item.get("attributes", {})
                domain = attrs.get("host_name", "")
                date   = attrs.get("date", 0)
                if domain and _DOMAIN_RE.match(domain):
                    results.append({"domain": domain, "last_seen": date})
            return results
    except Exception as exc:
        log.debug("VT IP resolutions failed for %s: %s", ip, exc)
        return []


# ── Public API ────────────────────────────────────────────────────────────────

async def passive_dns_domain(graph_db, domain: str) -> dict:
    """
    Enumerate subdomains and IP resolution history for a domain.

    Sources:
      • HackerTarget hostsearch (free)
      • VirusTotal passive DNS (if key configured)

    Merges discovered subdomains as Domain nodes and IPs as IP nodes,
    creating HAS_SUBDOMAIN and RESOLVED_TO relationships.
    """
    import asyncio
    ht_results, vt_results = await asyncio.gather(
        _ht_hostsearch(domain),
        _vt_domain_resolutions(domain),
        return_exceptions=True,
    )
    if isinstance(ht_results, Exception):
        ht_results = []
    if isinstance(vt_results, Exception):
        vt_results = []

    # Deduplicate IPs from HackerTarget
    seen_subs: set[str] = set()
    seen_ips:  set[str] = set()
    subdomains: list[dict] = []
    ip_history: list[str] = []

    for item in ht_results:
        sub = item["subdomain"].lower()
        ip  = item["ip"]
        if sub not in seen_subs and sub != domain.lower():
            seen_subs.add(sub)
            subdomains.append({"subdomain": sub, "ip": ip})
        if ip not in seen_ips:
            seen_ips.add(ip)
            ip_history.append(ip)

    for item in vt_results:
        ip = item["ip"]
        if ip not in seen_ips:
            seen_ips.add(ip)
            ip_history.append(ip)

    # Persist to graph
    async with graph_db.driver.session() as session:
        parent_id = await _merge_domain(session, domain)

        # Subdomains
        for item in subdomains[:50]:
            sub_id = await _merge_domain(session, item["subdomain"])
            await session.run("""
                MATCH (p:Domain {id: $pid}), (s:Domain {id: $sid})
                MERGE (p)-[:HAS_SUBDOMAIN]->(s)
            """, pid=parent_id, sid=sub_id)
            # Also link subdomain → IP
            if item["ip"]:
                ip_id = await _merge_ip(session, item["ip"])
                await session.run("""
                    MATCH (s:Domain {id: $sid}), (i:IP {id: $iid})
                    MERGE (s)-[:RESOLVED_TO]->(i)
                """, sid=sub_id, iid=ip_id)

        # Historical IPs the parent domain resolved to
        for ip in ip_history[:20]:
            ip_id = await _merge_ip(session, ip)
            await session.run("""
                MATCH (d:Domain {id: $did}), (i:IP {id: $iid})
                MERGE (d)-[:RESOLVED_TO]->(i)
            """, did=parent_id, iid=ip_id)

    return {
        "domain":     domain,
        "subdomains": subdomains[:50],
        "ip_history": ip_history[:20],
        "sources":    ["hackertarget"] + (["virustotal"] if vt_results else []),
    }


async def reverse_ip_lookup(graph_db, ip: str) -> dict:
    """
    Find domains co-hosted on the same IP.

    Sources:
      • HackerTarget reverseiplookup (free)
      • VirusTotal IP resolutions (if key configured)

    Merges discovered domains as Domain nodes with HOSTED_ON relationships.
    """
    import asyncio
    ht_domains, vt_domains = await asyncio.gather(
        _ht_reverse_ip(ip),
        _vt_ip_resolutions(ip),
        return_exceptions=True,
    )
    if isinstance(ht_domains, Exception):
        ht_domains = []
    if isinstance(vt_domains, Exception):
        vt_domains = []

    seen: set[str] = set()
    all_domains: list[str] = []
    for d in ht_domains:
        dl = d.lower()
        if dl not in seen:
            seen.add(dl)
            all_domains.append(d)
    for item in vt_domains:
        dl = item["domain"].lower()
        if dl not in seen:
            seen.add(dl)
            all_domains.append(item["domain"])

    async with graph_db.driver.session() as session:
        ip_id = await _merge_ip(session, ip)
        for dom in all_domains[:40]:
            dom_id = await _merge_domain(session, dom)
            await session.run("""
                MATCH (d:Domain {id: $did}), (i:IP {id: $iid})
                MERGE (d)-[:HOSTED_ON]->(i)
            """, did=dom_id, iid=ip_id)

    return {
        "ip":      ip,
        "domains": all_domains[:40],
        "count":   len(all_domains),
        "sources": ["hackertarget"] + (["virustotal"] if vt_domains else []),
    }

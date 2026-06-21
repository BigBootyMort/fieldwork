"""
crt.sh certificate-transparency crawler.

Phase 36 also adds `domain_cert_transparency(graph_db, domain)` —
a standalone async function that queries crt.sh for all certificates
issued for *.{domain}, persists discovered subdomains as Domain nodes
(DISCOVERED_VIA_CERT relationships), and returns structured results.
No API key required.

Queries the crt.sh database for TLS certificates whose subject
Organisation field matches the company name. Maps discovered
hostnames → Domain nodes linked to the Company.

Most useful with a company_hint — OV/EV certificates include the
organisation in the subject; DV certs (e.g. Let's Encrypt) do not.
Falls back to searching by person name if no company is given, though
results will be less reliable.

No API key required. crt.sh is a public service; be polite (1 req/2s).
"""
import httpx
import re
import logging
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("crawler.crtsh")

CRTSH_URL = "https://crt.sh/"

# Basic domain validation — rejects wildcards, raw IPs, and junk strings
_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


class CrtShCrawler:
    name = "crt.sh"

    def __init__(self, graph_db):
        self.graph = graph_db
        self.limiter = AsyncLimiter(max_rate=1, time_period=2)

    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        query = company_hint or person["name"]

        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "fieldwork-osint/0.2"},
            follow_redirects=True,
        ) as client:
            async with self.limiter:
                resp = await client.get(
                    CRTSH_URL,
                    params={"q": f"%{query}%", "output": "json"},
                )

            if resp.status_code != 200:
                log.warning("crt.sh: HTTP %s for %r", resp.status_code, query)
                return

            try:
                certs = resp.json()
            except Exception:
                log.warning("crt.sh: non-JSON response for %r", query)
                return

        # Collect unique, valid domain names across all certificates.
        # name_value can hold multiple newline-separated SANs.
        seen: set[str] = set()
        for cert in certs[:300]:
            for raw in (cert.get("name_value") or "").split("\n"):
                domain = raw.strip().lstrip("*.").lower()
                if domain and domain not in seen and _DOMAIN_RE.match(domain):
                    seen.add(domain)

        log.info("crt.sh: %d unique domains for %r", len(seen), query)
        if not seen:
            return

        # Resolve the owning company node when context is available
        company_id: Optional[str] = None
        if company_hint:
            company = await self.graph.add_company(company_hint)
            company_id = company["id"]

        async with self.graph.driver.session() as session:
            for domain in list(seen)[:50]:
                await session.run(
                    "MERGE (d:Domain {id: $id}) "
                    "ON CREATE SET d.name = $name, d.source = 'crt.sh', d.first_seen = datetime() "
                    "ON MATCH  SET d.last_seen = datetime()",
                    id=domain, name=domain,
                )
                if company_id:
                    await session.run(
                        "MATCH (c:Company {id: $cid}) "
                        "MATCH (d:Domain {id: $did}) "
                        "MERGE (c)-[r:OWNS_DOMAIN]->(d) "
                        "ON CREATE SET r.source = 'crt.sh', r.first_seen = datetime()",
                        cid=company_id, did=domain,
                    )
                log.info("  + domain: %s", domain)


# ── Ph36 — standalone domain cert transparency ────────────────────────────────

import hashlib as _hashlib

_SUB_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9\-]+)+$", re.I)


def _ct_node_id(domain: str) -> str:
    return f"domain_{_hashlib.sha1(domain.lower().encode()).hexdigest()[:12]}"


def _ct_clean(raw: str, base: str) -> str | None:
    name = raw.strip().lstrip("*.").lower()
    if not name or name == base.lower():
        return None
    if not _SUB_RE.match(name):
        return None
    if not (name == base.lower() or name.endswith("." + base.lower())):
        return None
    return name


async def domain_cert_transparency(graph_db, domain: str) -> dict:
    """
    Query crt.sh for all TLS certificates ever issued for *.{domain}.

    Persists discovered subdomains as Domain nodes connected to the
    parent domain via DISCOVERED_VIA_CERT relationships.

    Returns:
        {domain, subdomains:[{name, issuer, not_before, not_after}],
         cert_count, unique_count}
    """
    url = f"https://crt.sh/?q=%.{domain}&output=json"
    try:
        async with httpx.AsyncClient(
            timeout=25.0,
            headers={"User-Agent": "Fieldwork OSINT"},
            follow_redirects=True,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            rows = r.json()
    except Exception as exc:
        log.warning("crt.sh domain query failed for %s: %s", domain, exc)
        return {
            "domain": domain, "error": str(exc),
            "subdomains": [], "cert_count": 0, "unique_count": 0,
        }

    seen: dict[str, dict] = {}
    for row in rows:
        raw_names = (row.get("name_value") or row.get("common_name") or "").split("\n")
        for raw in raw_names:
            name = _ct_clean(raw, domain)
            if not name or name in seen:
                continue
            seen[name] = {
                "name":       name,
                "issuer":     (row.get("issuer_name") or "")[:120],
                "not_before": (row.get("not_before") or "")[:10],
                "not_after":  (row.get("not_after")  or "")[:10],
            }

    subdomains = sorted(seen.values(), key=lambda x: x["name"])[:100]

    # Persist
    parent_id = _ct_node_id(domain)
    async with graph_db.driver.session() as session:
        await session.run("""
            MERGE (d:Domain {id: $id})
            ON CREATE SET d.domain = $dom, d.name = $dom,
                          d.source = 'crtsh', d.created_at = datetime()
            ON MATCH SET  d.domain = $dom
        """, id=parent_id, dom=domain)

        for sub in subdomains:
            sid = _ct_node_id(sub["name"])
            await session.run("""
                MERGE (s:Domain {id: $id})
                ON CREATE SET s.domain = $name, s.name = $name,
                              s.source = 'crtsh', s.created_at = datetime()
                ON MATCH SET  s.domain = $name
            """, id=sid, name=sub["name"])
            await session.run("""
                MATCH (p:Domain {id: $pid}), (s:Domain {id: $sid})
                MERGE (p)-[r:DISCOVERED_VIA_CERT]->(s)
                ON CREATE SET r.issuer     = $issuer,
                              r.not_before = $nb,
                              r.not_after  = $na
            """, pid=parent_id, sid=sid,
                 issuer=sub["issuer"], nb=sub["not_before"], na=sub["not_after"])

    return {
        "domain":       domain,
        "subdomains":   subdomains,
        "cert_count":   len(rows),
        "unique_count": len(subdomains),
    }

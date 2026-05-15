"""
crt.sh certificate-transparency crawler.

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

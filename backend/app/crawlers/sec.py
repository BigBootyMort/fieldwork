"""
SEC EDGAR crawler — finds corporate filings that name a person.

Uses the EDGAR EFTS full-text search API (efts.sec.gov) to find proxy
statements and beneficial-ownership/insider-transaction filings that
mention the target. Creates Company nodes for each filer, with a
relationship that reflects the filing type:

  DEF 14A  (proxy statement — names directors and named execs)  → BOARD_MEMBER_OF
  SC 13D/G (beneficial ownership — large shareholders)          → INVESTOR_IN
  Form 4   (insider transaction — officer/director trades)      → OFFICER_OF

No API key required. SEC fair-use policy requires a descriptive
User-Agent; configure SEC_USER_AGENT in .env or it defaults to a
safe placeholder.
"""
import httpx
import os
import logging
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("crawler.sec")

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_USER_AGENT = os.getenv("SEC_USER_AGENT", "Fieldwork OSINT research@fieldwork.local")


class SECCrawler:
    name = "sec_edgar"

    def __init__(self, graph_db):
        self.graph = graph_db
        # SEC allows up to 10 req/sec; we run at 5 to leave headroom.
        self.limiter = AsyncLimiter(max_rate=5, time_period=1)

    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        name = person["name"]

        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
        ) as client:
            async with self.limiter:
                resp = await client.get(EFTS_URL, params={
                    "q": f'"{name}"',
                    "forms": "DEF 14A,SC 13D,SC 13G,4",
                    "dateRange": "custom",
                    "startdt": "2015-01-01",
                    "from": "0",
                    "size": "25",
                })

            if resp.status_code == 429:
                log.warning("SEC EDGAR: rate limited")
                return
            if resp.status_code != 200:
                log.warning("SEC EDGAR: HTTP %s for %r", resp.status_code, name)
                return

            try:
                data = resp.json()
            except Exception:
                log.warning("SEC EDGAR: non-JSON response")
                return

        hits = ((data.get("hits") or {}).get("hits")) or []
        log.info("SEC EDGAR: %d filing hits for %r", len(hits), name)

        seen: set[str] = set()
        for hit in hits[:25]:
            source = (hit.get("_source") or {})
            company_name = (source.get("entity_name") or "").strip()
            form_type = (source.get("form_type") or "").strip()
            if not company_name or company_name in seen:
                continue
            seen.add(company_name)

            if form_type == "4":
                rel = "OFFICER_OF"
            elif form_type in ("SC 13D", "SC 13G"):
                rel = "INVESTOR_IN"
            else:
                rel = "BOARD_MEMBER_OF"

            try:
                company = await self.graph.add_company(company_name)
                await self.graph.add_relationship(
                    person["id"], company["id"], rel, source="sec_edgar"
                )
                log.info("  + %s -> [%s] -> %s (%s)", name, rel, company_name, form_type)
            except ValueError as e:
                log.warning("Skipped: %s", e)

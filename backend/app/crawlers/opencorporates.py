"""
OpenCorporates crawler.

Fixes vs original:
  - Reuses the app-level GraphDB driver (passed in via constructor) instead
    of opening its own connection on every crawl.
  - Rate-limits requests (free tier: 50/day, 5/sec).
  - Uses add_relationship() with an allowlisted rel type.
"""
import httpx
import os
import logging
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("crawler.opencorporates")


class OpenCorporatesCrawler:
    name = "opencorporates"

    def __init__(self, graph_db):
        self.graph = graph_db
        self.api_key = os.getenv("OPENCORPORATES_TOKEN", "")
        self.base_url = "https://api.opencorporates.com/v0.4"
        # Free tier: 5 req/sec, 500/month. Be polite.
        self.limiter = AsyncLimiter(max_rate=3, time_period=1)

    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        if not self.api_key:
            log.info("OpenCorporates: no token, skipping")
            return

        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "fieldwork-osint/0.2"}) as client:
            params = {"q": person["name"], "api_token": self.api_key, "per_page": 25}
            if company_hint:
                params["company_name"] = company_hint

            log.info("OpenCorporates: searching for %r", person["name"])
            async with self.limiter:
                resp = await client.get(f"{self.base_url}/officers/search", params=params)

            if resp.status_code == 401:
                log.error("OpenCorporates: invalid API token")
                return
            if resp.status_code == 403:
                log.error("OpenCorporates: quota exceeded")
                return
            if resp.status_code != 200:
                log.warning("OpenCorporates: HTTP %s", resp.status_code)
                return

            data = resp.json()
            officers = (data.get("results", {}) or {}).get("officers", []) or []
            log.info("OpenCorporates: %d officer hits", len(officers))

            for officer_wrap in officers[:25]:
                officer = officer_wrap.get("officer", {}) if isinstance(officer_wrap, dict) else {}
                company_data = officer.get("company") or {}
                company_name = company_data.get("name")
                position = (officer.get("position") or "").lower()
                if not company_name:
                    continue

                company = await self.graph.add_company(company_name)

                # Choose relationship by position
                rel = "OFFICER_OF"
                if any(k in position for k in ("director", "board")):
                    rel = "BOARD_MEMBER_OF"
                elif "employee" in position:
                    rel = "WORKS_AT"

                try:
                    await self.graph.add_relationship(person["id"], company["id"], rel, source="opencorporates")
                    log.info("  + %s -> [%s] -> %s", person["name"], rel, company_name)
                except ValueError as e:
                    log.warning("Skipped relationship: %s", e)

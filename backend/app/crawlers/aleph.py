"""
OCCRP Aleph crawler.

Searches the Aleph investigative journalism database for a person or
company. Aleph aggregates 130+ source collections: corporate registries,
court records, sanctions lists, leaked datasets and document archives —
covering jurisdictions that OpenCorporates and SEC EDGAR don't touch.

Free account + API key: https://aleph.occrp.org

What we extract:
  Person entities   → Person nodes (MENTIONED_WITH)
  Company/Org       → Company nodes (RELATED_TO via add_relationship)
  Dataset label     → stored as relationship property so you know the source

Rate limits: no hard published limit on the public instance; we stay
at 2 req/s to be polite to a shared research infrastructure.
"""
import httpx
import os
import logging
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("crawler.aleph")

ALEPH_BASE = "https://aleph.occrp.org/api/2"

# Schema names we map to graph node types
_PERSON_SCHEMAS  = {"Person", "LegalEntity"}
_COMPANY_SCHEMAS = {"Company", "Organization", "PublicBody", "Vessel", "Asset"}


class AlephCrawler:
    name = "aleph"

    def __init__(self, graph_db):
        self.graph = graph_db
        self.api_key = os.getenv("ALEPH_API_KEY", "")
        self.limiter = AsyncLimiter(max_rate=2, time_period=1)

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "fieldwork-osint/0.3"}
        if self.api_key:
            h["Authorization"] = f"ApiKey {self.api_key}"
        return h

    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        name = person["name"]
        results = await self._search(name)

        if company_hint:
            company_results = await self._search(company_hint)
            # Deduplicate by Aleph entity id
            seen = {r["id"] for r in results}
            results += [r for r in company_results if r["id"] not in seen]

        log.info("Aleph: %d entities for %r", len(results), name)

        for entity in results[:40]:
            schema = entity.get("schema", "")
            props   = entity.get("properties", {})
            caption = (entity.get("caption") or "").strip()
            datasets = ", ".join(entity.get("datasets", []))[:200]

            if not caption:
                continue

            if schema in _COMPANY_SCHEMAS:
                try:
                    company = await self.graph.add_company(caption)
                    await self.graph.add_relationship(
                        person["id"], company["id"], "RELATED_TO",
                        source=f"aleph:{datasets[:100]}",
                    )
                    log.info("  + %s -> [RELATED_TO] -> %s (aleph)", name, caption)
                except ValueError as e:
                    log.warning("Skipped: %s", e)

            elif schema in _PERSON_SCHEMAS and caption.lower() != name.lower():
                # Another person found in the same documents as our target
                async with self.graph.driver.session() as session:
                    from graph import slugify
                    other_id = slugify(caption)
                    await session.run(
                        "MERGE (o:Person {id: $id}) "
                        "ON CREATE SET o.name = $name, o.created_at = datetime() "
                        "WITH o MATCH (p:Person {id: $pid}) "
                        "MERGE (p)-[r:MENTIONED_WITH]->(o) "
                        "ON CREATE SET r.source = $src, r.first_seen = datetime()",
                        id=other_id, name=caption, pid=person["id"],
                        src=f"aleph:{datasets[:100]}",
                    )
                    log.info("  + %s co-mentioned with %s (aleph)", name, caption)

    async def _search(self, query: str) -> list[dict]:
        """Search Aleph entities by name. Returns up to 40 results."""
        async with httpx.AsyncClient(
            timeout=20.0, headers=self._headers(), follow_redirects=True
        ) as client:
            async with self.limiter:
                resp = await client.get(f"{ALEPH_BASE}/entities", params={
                    "q": query,
                    "limit": 40,
                    "filter:schemata": "Person,Company,Organization,PublicBody,LegalEntity",
                })

            if resp.status_code == 401:
                log.error("Aleph: invalid or missing API key")
                return []
            if resp.status_code == 429:
                log.warning("Aleph: rate limited")
                return []
            if resp.status_code != 200:
                log.warning("Aleph: HTTP %s for %r", resp.status_code, query)
                return []

            data = resp.json()
            return (data.get("results") or [])


async def search_aleph(query: str, schema_filter: str = "") -> list[dict]:
    """
    Standalone search helper used by the /search/aleph endpoint.
    Returns raw Aleph entity dicts, not graph nodes.
    """
    api_key = os.getenv("ALEPH_API_KEY", "")
    headers = {"Accept": "application/json", "User-Agent": "fieldwork-osint/0.3"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    params: dict = {"q": query, "limit": 40}
    if schema_filter:
        params["filter:schemata"] = schema_filter

    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.get(f"{ALEPH_BASE}/entities", params=params)

    if resp.status_code != 200:
        return []

    return resp.json().get("results") or []

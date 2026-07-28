"""
GitHub public-profile crawler.

Searches GitHub users by name, creates Username + Account nodes for
credible matches, and links their public organisations as Company nodes.

Rate limits (requests/min):
  unauthenticated — 10 (search), 60/hour (other endpoints)
  with GITHUB_TOKEN — 30 (search), 5000/hour (other)
We run at 1 req/sec to stay safe on both tiers.
"""
import httpx
import os
import logging
from typing import Optional
from aiolimiter import AsyncLimiter

log = logging.getLogger("crawler.github")

GITHUB_API = "https://api.github.com"


class GitHubCrawler:
    name = "github"

    def __init__(self, graph_db):
        self.graph = graph_db
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.limiter = AsyncLimiter(max_rate=1, time_period=1)

    def _headers(self) -> dict:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fieldwork-osint/0.2",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        name = person["name"]
        query = f'"{name}" in:name type:user'

        # Phase 1: fetch all data before writing to Neo4j
        users_data: list[dict] = []
        async with httpx.AsyncClient(
            timeout=20.0, headers=self._headers(), follow_redirects=True
        ) as client:
            async with self.limiter:
                resp = await client.get(
                    f"{GITHUB_API}/search/users",
                    params={"q": query, "per_page": 5},
                )

            if resp.status_code == 401:
                log.error("GitHub: bad token")
                return
            if resp.status_code == 403:
                log.warning("GitHub: rate-limited or token scope issue")
                return
            if resp.status_code != 200:
                log.warning("GitHub: HTTP %s for %r", resp.status_code, name)
                return

            users = resp.json().get("items", [])
            log.info("GitHub: %d user hits for %r", len(users), name)

            for user in users[:5]:
                login = user.get("login", "")
                profile_url = user.get("html_url", "")
                if not login:
                    continue

                # Public org memberships → potential Company nodes
                async with self.limiter:
                    orgs_resp = await client.get(
                        f"{GITHUB_API}/users/{login}/orgs",
                        params={"per_page": 10},
                    )
                orgs = orgs_resp.json() if orgs_resp.status_code == 200 else []

                users_data.append({
                    "login": login,
                    "profile_url": profile_url,
                    "orgs": [o.get("login") or o.get("name") for o in orgs[:5] if o.get("login") or o.get("name")],
                })

        # Phase 2: write to Neo4j
        async with self.graph.driver.session() as session:
            for ud in users_data:
                login = ud["login"]
                profile_url = ud["profile_url"]

                await session.run(
                    "MERGE (u:Username {id: $id}) "
                    "ON CREATE SET u.handle = $handle, u.platform = 'github', "
                    "              u.url = $url, u.first_seen = datetime() "
                    "WITH u "
                    "MATCH (p:Person {id: $pid}) "
                    "MERGE (p)-[r:USES_HANDLE]->(u) "
                    "ON CREATE SET r.source = 'github', r.first_seen = datetime()",
                    id=f"github:{login}", handle=login, url=profile_url, pid=person["id"],
                )

                await session.run(
                    "MERGE (a:Account {id: $id}) "
                    "ON CREATE SET a.url = $url, a.platform = 'github', "
                    "              a.username = $handle, a.first_seen = datetime() "
                    "WITH a "
                    "MATCH (p:Person {id: $pid}) "
                    "MERGE (p)-[r:HAS_ACCOUNT]->(a) "
                    "ON CREATE SET r.source = 'github', r.first_seen = datetime()",
                    id=profile_url, url=profile_url, handle=login, pid=person["id"],
                )
                log.info("  + %s -> [USES_HANDLE] -> github:%s", name, login)

        # Link GitHub orgs as Company nodes via the existing allowlisted helper
        for ud in users_data:
            for org_name in ud["orgs"]:
                try:
                    company = await self.graph.add_company(org_name)
                    await self.graph.add_relationship(
                        person["id"], company["id"], "WORKS_AT", source="github"
                    )
                    log.info("  + %s -> [WORKS_AT] -> %s (github org)", name, org_name)
                except ValueError as e:
                    log.warning("Skipped org relationship: %s", e)


# ── Standalone read-only variant (for the orchestrator; no graph writes) ──────

async def search_github_users(name: str, limit: int = 5) -> dict:
    """Search GitHub users by name and return structured matches — no Neo4j
    writes. Returns {found, count, users:[{login, profile_url, orgs:[…]}]} or a
    blind/{error} soft result so the orchestrator's coverage can classify it."""
    token = os.getenv("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fieldwork-osint/0.2",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    limiter = AsyncLimiter(max_rate=1, time_period=1)
    query = f'"{name}" in:name type:user'

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers,
                                     follow_redirects=True) as client:
            async with limiter:
                resp = await client.get(f"{GITHUB_API}/search/users",
                                        params={"q": query, "per_page": limit})
            if resp.status_code in (401, 403):
                return {"found": False,
                        "reason": f"no token / rate-limited (HTTP {resp.status_code}) "
                                  f"— set GITHUB_TOKEN"}
            if resp.status_code != 200:
                return {"error": f"GitHub HTTP {resp.status_code}"}

            users = resp.json().get("items", [])[:limit]
            out: list[dict] = []
            for user in users:
                login = user.get("login", "")
                if not login:
                    continue
                async with limiter:
                    orgs_resp = await client.get(
                        f"{GITHUB_API}/users/{login}/orgs", params={"per_page": 10})
                orgs = orgs_resp.json() if orgs_resp.status_code == 200 else []
                out.append({
                    "login": login,
                    "profile_url": user.get("html_url", ""),
                    "orgs": [o.get("login") or o.get("name")
                             for o in orgs[:5] if o.get("login") or o.get("name")],
                })
    except Exception as exc:
        return {"error": str(exc)}

    return {"found": bool(out), "count": len(out), "users": out}

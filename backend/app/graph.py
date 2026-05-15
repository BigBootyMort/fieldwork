"""
Neo4j wrapper for Fieldwork.

Fixes vs the original:
  - Async driver is shared at app level (lifespan), not recreated per call.
  - Returned entities include labels[] so the frontend can disambiguate.
  - find_weak_links uses dict access, not positional indexing.
  - Relationship types come from an allowlist; never interpolated from input.
  - Person IDs are slug-validated.
"""
from neo4j import AsyncGraphDatabase
import asyncio
import logging
import os
import re
import unicodedata
from typing import List, Dict, Any, Optional

log = logging.getLogger("fieldwork.graph")

ALLOWED_RELS = {
    "BOARD_MEMBER_OF",
    "OFFICER_OF",
    "WORKS_AT",
    "FOUNDED",
    "INVESTOR_IN",
    "MENTIONED_WITH",
    "RELATED_TO",
}


def slugify(name: str) -> str:
    """Make a safe, deterministic ID from a human name."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-zA-Z0-9]+", "_", n).strip("_").lower()
    return n[:180] or "unknown"


class GraphDB:
    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "changeme_dev_only")
        self.driver = None

    async def connect(self, retries: int = 10, delay: float = 3.0):
        """Connect with retries so hot-reloads survive a momentary Neo4j blip."""
        self.driver = AsyncGraphDatabase.driver(self.uri, auth=(self.user, self.password))
        for attempt in range(1, retries + 1):
            try:
                await self.driver.verify_connectivity()
                return
            except Exception as exc:
                if attempt == retries:
                    raise
                log.warning("Neo4j not ready (attempt %d/%d): %s — retrying in %.0fs",
                            attempt, retries, exc, delay)
                await asyncio.sleep(delay)

    async def close(self):
        if self.driver:
            await self.driver.close()
            self.driver = None

    async def ping(self) -> bool:
        try:
            async with self.driver.session() as s:
                await s.run("RETURN 1")
            return True
        except Exception:
            return False

    # ---- helpers ----
    @staticmethod
    def _entity_dict(node) -> Dict[str, Any]:
        """Serialize a Neo4j node with its labels."""
        d = dict(node)
        d["labels"] = list(node.labels)
        return d

    # ---- person ops ----
    async def find_or_create_person(self, name: str) -> Dict:
        pid = slugify(name)
        async with self.driver.session() as s:
            res = await s.run(
                "MERGE (p:Person {id: $id}) "
                "ON CREATE SET p.name = $name, p.created_at = datetime() "
                "ON MATCH SET p.name = coalesce(p.name, $name) "
                "RETURN p",
                id=pid, name=name,
            )
            rec = await res.single()
            return self._entity_dict(rec["p"])

    async def get_person_by_id(self, person_id: str) -> Optional[Dict]:
        async with self.driver.session() as s:
            res = await s.run("MATCH (p:Person {id: $id}) RETURN p", id=person_id)
            rec = await res.single()
            return self._entity_dict(rec["p"]) if rec else None

    async def search_person(
        self, name: str, company: Optional[str] = None, location: Optional[str] = None
    ) -> List[Dict]:
        async with self.driver.session() as s:
            params: Dict[str, Any] = {"name": name}
            q = "MATCH (p:Person) WHERE toLower(p.name) CONTAINS toLower($name)"
            if company:
                q += " AND EXISTS { MATCH (p)-[]->(c:Company) WHERE toLower(c.name) CONTAINS toLower($company) }"
                params["company"] = company
            q += " RETURN p LIMIT 25"
            res = await s.run(q, params)
            recs = await res.values()
            return [self._entity_dict(r[0]) for r in recs]

    async def get_connections(self, person_id: str, depth: int = 2) -> List[Dict]:
        # depth is interpolated but bounded — Cypher does not accept it as a parameter for var-length paths
        depth = max(1, min(int(depth), 4))
        async with self.driver.session() as s:
            q = f"""
            MATCH path = (start:Person {{id: $person_id}})-[*1..{depth}]-(connected)
            WHERE (connected:Person OR connected:Company) AND connected <> start
            WITH DISTINCT connected, min(length(path)) AS dist, head(collect([rel IN relationships(path) | type(rel)])) AS path_types
            RETURN connected AS entity, dist AS distance, path_types
            ORDER BY dist, entity.name
            LIMIT 200
            """
            res = await s.run(q, person_id=person_id)
            out: List[Dict] = []
            async for rec in res:
                out.append({
                    "entity": self._entity_dict(rec["entity"]),
                    "distance": rec["distance"],
                    "path_types": rec["path_types"] or [],
                })
            return out

    async def find_paths(self, source_id: str, target_id: str, max_depth: int = 3) -> List[Dict]:
        max_depth = max(1, min(int(max_depth), 6))
        async with self.driver.session() as s:
            q = f"""
            MATCH path = allShortestPaths((a:Person {{id: $source}})-[*..{max_depth}]-(b {{id: $target}}))
            RETURN [n IN nodes(path) | coalesce(n.name, n.id)] AS path,
                   [r IN relationships(path) | type(r)] AS edges
            LIMIT 10
            """
            res = await s.run(q, source=source_id, target=target_id)
            recs = await res.values()
            return [{"path": r[0], "edges": r[1]} for r in recs]

    async def find_weak_links(self, person_id: str) -> List[Dict]:
        """
        Heuristic: people connected via shared board memberships who themselves
        have few other board roles. The reasoning is contestable — present
        results as suggestions, not assertions.
        """
        async with self.driver.session() as s:
            q = """
            MATCH (target:Person {id: $person_id})-[:BOARD_MEMBER_OF|OFFICER_OF]->(c:Company)
            MATCH (other:Person)-[:BOARD_MEMBER_OF|OFFICER_OF]->(c)
            WHERE other <> target
            WITH other, count(DISTINCT c) AS shared_companies
            OPTIONAL MATCH (other)-[:BOARD_MEMBER_OF|OFFICER_OF]->(allc:Company)
            WITH other, shared_companies, count(DISTINCT allc) AS total_boards
            WITH other, shared_companies, total_boards,
                 CASE
                   WHEN total_boards <= 1 THEN 'high'
                   WHEN total_boards <= 3 THEN 'medium'
                   ELSE 'low'
                 END AS confidence
            RETURN other AS person,
                   other.name AS name,
                   shared_companies,
                   total_boards,
                   confidence
            ORDER BY
              CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
              shared_companies DESC
            LIMIT 25
            """
            res = await s.run(q, person_id=person_id)
            out: List[Dict] = []
            async for rec in res:
                out.append({
                    "person": self._entity_dict(rec["person"]),
                    "name": rec["name"],
                    "shared_companies": rec["shared_companies"],
                    "total_boards": rec["total_boards"],
                    "confidence": rec["confidence"],
                })
            return out

    # ---- mutation helpers (used by crawlers) ----
    async def add_company(self, company_name: str) -> Dict:
        cid = slugify(company_name)
        async with self.driver.session() as s:
            res = await s.run(
                "MERGE (c:Company {id: $id}) "
                "ON CREATE SET c.name = $name, c.created_at = datetime() "
                "ON MATCH SET c.name = coalesce(c.name, $name) "
                "RETURN c",
                id=cid, name=company_name,
            )
            rec = await res.single()
            return self._entity_dict(rec["c"])

    async def add_relationship(
        self, person_id: str, company_id: str, rel_type: str, source: str = "unknown"
    ):
        """Add P-[REL]->C with a source tag. Rel type must be in the allowlist."""
        if rel_type not in ALLOWED_RELS:
            raise ValueError(f"Disallowed relationship type: {rel_type}")
        async with self.driver.session() as s:
            await s.run(
                f"""
                MATCH (p:Person {{id: $pid}})
                MATCH (c:Company {{id: $cid}})
                MERGE (p)-[r:{rel_type}]->(c)
                ON CREATE SET r.source = $source, r.first_seen = datetime()
                ON MATCH  SET r.last_seen = datetime()
                """,
                pid=person_id, cid=company_id, source=source,
            )

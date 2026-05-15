"""
SpiderFoot integration.

Two parts:
  1. Proxy: Tiny client that talks to the SpiderFoot OSS web endpoints.
     These are internal endpoints — undocumented as public API but stable since
     v4.0 (April 2022, last release).
  2. Promote: Map a subset of SpiderFoot's ~200 event types into our Neo4j
     schema. We don't map everything — that's a recipe for noisy/wrong data.
     We focus on ~25 event types most useful for journalism work.
"""
from __future__ import annotations
import os
import logging
from typing import Optional, Any
import httpx

log = logging.getLogger("spiderfoot")

SPIDERFOOT_URL = os.getenv("SPIDERFOOT_URL", "http://spiderfoot:5001")

# Map SpiderFoot use-case names to API param values
USECASES = {"passive", "investigate", "footprint", "all"}


# === Event-type → graph mapping ===
#
# Each entry: (NodeLabel, IdProperty, RelationshipToTarget, RelDirection)
#
# Direction: 'out' = (target)-[REL]->(node), 'in' = (node)-[REL]->(target)
#
# We deliberately map a small, high-confidence subset. Other event types are
# preserved as 'raw_findings' on the case but NOT graph nodes — that would
# create noise (e.g. RAW_DNS_RECORDS would explode the graph).
EVENT_MAP: dict[str, tuple[str, str, str, str]] = {
    # Identity & contact
    "EMAILADDR":               ("Email",     "address",  "HAS_EMAIL",       "out"),
    "AFFILIATE_EMAILADDR":     ("Email",     "address",  "AFFILIATE_EMAIL", "out"),
    "PHONE_NUMBER":            ("Phone",     "number",   "HAS_PHONE",       "out"),
    "USERNAME":                ("Username",  "handle",   "USES_HANDLE",     "out"),
    "SOCIAL_MEDIA":            ("Account",   "url",      "HAS_ACCOUNT",     "out"),
    "ACCOUNT_EXTERNAL_OWNED":  ("Account",   "url",      "OWNS_ACCOUNT",    "out"),
    "HUMAN_NAME":              ("Person",    "name",     "MENTIONED_WITH",  "out"),
    "PERSON_NAME":             ("Person",    "name",     "MENTIONED_WITH",  "out"),
    "COMPANY_NAME":            ("Company",   "name",     "ASSOCIATED_WITH", "out"),

    # Network
    "IP_ADDRESS":              ("IP",        "address",  "RESOLVED_TO",     "out"),
    "IPV6_ADDRESS":            ("IP",        "address",  "RESOLVED_TO",     "out"),
    "DOMAIN_NAME":             ("Domain",    "name",     "OWNS_DOMAIN",     "out"),
    "INTERNET_NAME":           ("Domain",    "name",     "HAS_HOSTNAME",    "out"),
    "AFFILIATE_INTERNET_NAME": ("Domain",    "name",     "AFFILIATE_DOMAIN","out"),

    # Security / breach
    "PASSWORD_COMPROMISED":    ("Breach",    "key",      "EXPOSED_IN",      "out"),
    "ACCOUNT_BREACH":          ("Breach",    "key",      "EXPOSED_IN",      "out"),
    "BREACH_DATA":             ("Breach",    "key",      "EXPOSED_IN",      "out"),
    "LEAKSITE_CONTENT":        ("Leak",      "key",      "MENTIONED_IN",    "out"),

    # Geo
    "GEOINFO":                 ("Location",  "name",     "LOCATED_AT",      "out"),
    "PHYSICAL_ADDRESS":        ("Location",  "name",     "ADDRESS",         "out"),
    "PHYSICAL_COORDINATES":    ("Location",  "name",     "COORDINATES",     "out"),
    "COUNTRY_NAME":            ("Location",  "name",     "IN_COUNTRY",      "out"),

    # Crypto (useful for sanctions evasion work)
    "BITCOIN_ADDRESS":         ("Wallet",    "address",  "OWNS_WALLET",     "out"),
    "ETHEREUM_ADDRESS":        ("Wallet",    "address",  "OWNS_WALLET",     "out"),
}


class SpiderFootClient:
    """Thin async client over SpiderFoot's web endpoints.

    These endpoints power the SpiderFoot web UI; they accept form-encoded data
    and return JSON. Not a documented public API.
    """
    def __init__(self, base_url: str = SPIDERFOOT_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "fieldwork-engine/0.2"},
        )

    async def ping(self) -> bool:
        async with await self._client() as c:
            r = await c.get("/ping")
            return r.status_code == 200 and "SUCCESS" in r.text

    async def start_scan(self, name: str, target: str, target_type: str, usecase: str = "passive") -> str:
        """Returns the scan ID."""
        if usecase not in USECASES:
            raise ValueError(f"Invalid usecase: {usecase}")
        # SpiderFoot's /startscan accepts form-encoded params.
        # 'typelist' / 'modulelist' empty means use the usecase's defaults.
        async with await self._client() as c:
            r = await c.post(
                "/startscan",
                data={
                    "scanname": name,
                    "scantarget": target,
                    "usecase": usecase,
                    "modulelist": "",
                    "typelist": "",
                },
            )
            # SpiderFoot's /startscan redirects to /scaninfo?id=<scanid> on success.
            # If we got a 200 back through redirects, find the scan ID in the URL or response.
            # Easier: list scans and find the newest matching name.
            if r.status_code >= 400:
                raise RuntimeError(f"SpiderFoot /startscan returned {r.status_code}: {r.text[:300]}")

            # Try to extract from URL (it redirects to /scaninfo?id=...)
            if "id=" in str(r.url):
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(str(r.url)).query)
                if "id" in qs:
                    return qs["id"][0]

            # Fallback: pull list and find by name
            r2 = await c.get("/scanlist")
            scans = r2.json()
            for s in reversed(scans):  # newest last
                if isinstance(s, list) and len(s) >= 2 and s[1] == name:
                    return s[0]
            raise RuntimeError("Could not determine scan ID after /startscan")

    async def scan_status(self, scan_id: str) -> dict:
        async with await self._client() as c:
            r = await c.get("/scanopts", params={"id": scan_id})
            r.raise_for_status()
            data = r.json()
            # /scanopts returns scan metadata including status
            meta = data.get("meta", [])
            # meta is [target, name, created, started, ended, status]
            status = meta[5] if len(meta) >= 6 else "UNKNOWN"
            return {
                "scan_id": scan_id,
                "target": meta[0] if meta else "",
                "name": meta[1] if len(meta) >= 2 else "",
                "started": meta[3] if len(meta) >= 4 else "",
                "ended": meta[4] if len(meta) >= 5 else "",
                "status": status,
            }

    async def scan_results(self, scan_id: str, event_type: str = "ALL") -> list[dict]:
        async with await self._client() as c:
            r = await c.post(
                "/scaneventresults",
                data={"id": scan_id, "eventType": event_type},
            )
            r.raise_for_status()
            rows = r.json()
            # Each row: [timestamp, data, source_data, module, type, ...]
            return [
                {
                    "ts": row[0] if len(row) > 0 else "",
                    "data": row[1] if len(row) > 1 else "",
                    "source_data": row[2] if len(row) > 2 else "",
                    "module": row[3] if len(row) > 3 else "",
                    "type": row[4] if len(row) > 4 else "",
                    "confidence": row[6] if len(row) > 6 else None,
                }
                for row in rows
                if isinstance(row, list)
            ]

    async def list_scans(self) -> list[dict]:
        async with await self._client() as c:
            r = await c.get("/scanlist")
            r.raise_for_status()
            rows = r.json()
            # Each row: [scan_id, name, target, created, started, ended, status, count]
            return [
                {
                    "id": row[0] if len(row) > 0 else "",
                    "name": row[1] if len(row) > 1 else "",
                    "target": row[2] if len(row) > 2 else "",
                    "started": row[4] if len(row) > 4 else "",
                    "status": row[6] if len(row) > 6 else "",
                    "event_count": row[7] if len(row) > 7 else 0,
                }
                for row in rows
                if isinstance(row, list)
            ]


async def promote_scan_to_graph(graph_db, scan_id: str, target_node_id: Optional[str] = None) -> dict:
    """
    Pull all events from a SpiderFoot scan and create nodes/edges in Neo4j
    for the ones in EVENT_MAP. Returns counts.

    The target_node_id is the existing Person/Company we attach these to.
    If not provided, we attach to a node named after the scan target.
    """
    client = SpiderFootClient()
    info = await client.scan_status(scan_id)
    events = await client.scan_results(scan_id)

    # Resolve the target node
    if target_node_id:
        target_id = target_node_id
    else:
        # Create a Person node for the scan target if we have a name-shaped target
        from graph import slugify
        target_id = slugify(info.get("target", scan_id))

    nodes_created = 0
    edges_created = 0
    skipped_types: set[str] = set()

    async with graph_db.driver.session() as session:
        for ev in events:
            event_type = ev.get("type", "")
            data = ev.get("data", "").strip()
            module = ev.get("module", "")
            if not data:
                continue
            if event_type not in EVENT_MAP:
                skipped_types.add(event_type)
                continue
            label, id_prop, rel, direction = EVENT_MAP[event_type]
            # Use a slug-ish ID for safety
            from graph import slugify
            node_id = slugify(data) if label in ("Person", "Company", "Location") else data.lower()[:200]

            if direction == "out":
                cypher = (
                    f"MERGE (n:{label} {{id: $nid}}) "
                    f"ON CREATE SET n.{id_prop} = $val, n.first_seen = datetime() "
                    f"ON MATCH SET n.{id_prop} = coalesce(n.{id_prop}, $val) "
                    f"WITH n "
                    f"MATCH (t {{id: $tid}}) "
                    f"MERGE (t)-[r:{rel}]->(n) "
                    f"ON CREATE SET r.source = $src, r.module = $mod, r.first_seen = datetime() "
                    f"ON MATCH SET r.last_seen = datetime() "
                    f"RETURN n, r"
                )
            else:
                cypher = (
                    f"MERGE (n:{label} {{id: $nid}}) "
                    f"ON CREATE SET n.{id_prop} = $val, n.first_seen = datetime() "
                    f"WITH n "
                    f"MATCH (t {{id: $tid}}) "
                    f"MERGE (n)-[r:{rel}]->(t) "
                    f"ON CREATE SET r.source = $src, r.module = $mod, r.first_seen = datetime() "
                    f"RETURN n, r"
                )
            try:
                result = await session.run(cypher, nid=node_id, val=data, tid=target_id, src=f"spiderfoot:{scan_id}", mod=module)
                summary = await result.consume()
                nodes_created += summary.counters.nodes_created
                edges_created += summary.counters.relationships_created
            except Exception as e:
                log.warning("Promote failed for %s/%s: %s", event_type, data[:50], e)

    return {
        "scan_id": scan_id,
        "target_id": target_id,
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "events_processed": len(events),
        "skipped_event_types": sorted(skipped_types),
    }

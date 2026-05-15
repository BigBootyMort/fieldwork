"""
Phase 7 — Continuous monitoring.

WatchedSubject CRUD, connection-diff alert creation, and per-watch check logic.
The scheduler (scheduler.py) calls check_watch() for each due subject on a
15-minute poll interval. A WatchedSubject stores the SHA-256 of its last known
connection set; any change causes Alert nodes to be written to the graph and
surfaced in the frontend bell icon.
"""
import hashlib
import logging
import re
from typing import Optional

log = logging.getLogger("fieldwork.monitor")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().strip()).strip("_")


# ── WatchedSubject CRUD ───────────────────────────────────────────────────────

async def add_watch(graph_db, name: str, interval_hours: int = 24) -> dict:
    """Create or activate a WatchedSubject for the given person name."""
    sid = f"watch_{_slug(name)}"
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MERGE (w:WatchedSubject {id: $id})
            ON CREATE SET
                w.name           = $name,
                w.interval_hours = $interval_hours,
                w.active         = true,
                w.created_at     = datetime(),
                w.last_checked   = null,
                w.snapshot_hash  = null
            ON MATCH SET
                w.active         = true,
                w.interval_hours = $interval_hours
            RETURN w { .* } AS w
            """,
            id=sid, name=name, interval_hours=interval_hours,
        )
        record = await result.single()
        return dict(record["w"])


async def remove_watch(graph_db, watch_id: str) -> bool:
    """Deactivate (soft-delete) a WatchedSubject. Returns False if not found."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (w:WatchedSubject {id: $id}) SET w.active = false RETURN w.id AS id",
            id=watch_id,
        )
        record = await result.single()
        return record is not None


async def list_watches(graph_db) -> list[dict]:
    """Return all WatchedSubject nodes sorted by name."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (w:WatchedSubject) RETURN w { .* } AS w ORDER BY w.name"
        )
        return [dict(r["w"]) async for r in result]


async def get_watch(graph_db, watch_id: str) -> Optional[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (w:WatchedSubject {id: $id}) RETURN w { .* } AS w",
            id=watch_id,
        )
        record = await result.single()
        return dict(record["w"]) if record else None


async def due_watches(graph_db) -> list[dict]:
    """
    Return active watches whose next check time is now due.
    Due = last_checked is null OR (last_checked + interval_hours) <= now.
    """
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (w:WatchedSubject {active: true})
            WHERE w.last_checked IS NULL
               OR w.last_checked + duration({hours: w.interval_hours}) <= datetime()
            RETURN w { .* } AS w
            """
        )
        return [dict(r["w"]) async for r in result]


async def all_active_watches(graph_db) -> list[dict]:
    """Return all active WatchedSubjects (for manual trigger)."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (w:WatchedSubject {active: true}) RETURN w { .* } AS w ORDER BY w.name"
        )
        return [dict(r["w"]) async for r in result]


async def _mark_checked(graph_db, watch_id: str, snapshot_hash: str) -> None:
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (w:WatchedSubject {id: $id})
            SET w.last_checked = datetime(), w.snapshot_hash = $hash
            """,
            id=watch_id, hash=snapshot_hash,
        )


# ── Snapshot + diff ───────────────────────────────────────────────────────────

async def _snapshot(graph_db, person_name: str) -> tuple[str, list[dict]]:
    """
    Capture all first-degree connections for a Person node by name.
    Returns (sha256_of_sorted_connection_ids, list_of_connection_dicts).
    """
    person_slug = _slug(person_name)
    async with graph_db.driver.session() as session:
        # Confirm the person node exists
        exists = await session.run(
            "MATCH (p:Person {id: $id}) RETURN p.id AS pid LIMIT 1",
            id=person_slug,
        )
        if not await exists.single():
            return "empty", []

        result = await session.run(
            """
            MATCH (p:Person {id: $pid})-[r]-(n)
            RETURN DISTINCT
                coalesce(n.id, toString(id(n))) AS nid,
                labels(n)[0]                    AS label,
                type(r)                         AS rel_type,
                coalesce(n.name, n.address, n.handle, n.id, '') AS name
            ORDER BY nid
            """,
            pid=person_slug,
        )
        connections = [
            {
                "id":       r["nid"],
                "label":    r["label"],
                "rel_type": r["rel_type"],
                "name":     r["name"],
            }
            async for r in result
        ]

    conn_ids = sorted(c["id"] for c in connections if c["id"])
    digest = hashlib.sha256("|".join(conn_ids).encode()).hexdigest()
    return digest, connections


async def _alerted_ids(graph_db, watch_id: str) -> set[str]:
    """Return the set of node IDs that have already generated an Alert for this watch."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (a:Alert {watch_id: $wid}) RETURN a.node_id AS nid",
            wid=watch_id,
        )
        return {r["nid"] async for r in result if r["nid"]}


async def _create_alerts(
    graph_db,
    watch: dict,
    new_connections: list[dict],
    already_alerted: set[str],
) -> int:
    """
    For every connection whose ID is not in already_alerted, write an Alert node.
    Returns the number of alerts created.
    """
    count = 0
    async with graph_db.driver.session() as session:
        for conn in new_connections:
            nid = conn.get("id") or ""
            if not nid or nid in already_alerted:
                continue
            # Stable ID: sha256 of watch_id + node_id (truncated to 20 hex chars)
            alert_id = "alert_" + hashlib.sha256(
                f'{watch["id"]}:{nid}'.encode()
            ).hexdigest()[:20]

            summary = (
                f"New {conn['label']}: {conn['name'] or nid} "
                f"linked to {watch['name']} via {conn['rel_type']}"
            )
            await session.run(
                """
                MERGE (a:Alert {id: $id})
                ON CREATE SET
                    a.watch_id   = $watch_id,
                    a.watch_name = $watch_name,
                    a.summary    = $summary,
                    a.node_id    = $node_id,
                    a.node_label = $node_label,
                    a.rel_type   = $rel_type,
                    a.created_at = datetime(),
                    a.seen       = false
                """,
                id=alert_id,
                watch_id=watch["id"],
                watch_name=watch["name"],
                summary=summary,
                node_id=nid,
                node_label=conn["label"],
                rel_type=conn["rel_type"],
            )
            count += 1
    return count


# ── Main check cycle ──────────────────────────────────────────────────────────

async def check_watch(graph_db, watch: dict) -> dict:
    """
    Run one monitoring cycle for a WatchedSubject.

    - Takes a new snapshot of the person's connections.
    - Compares the hash against the stored snapshot_hash.
    - If changed and there was a previous snapshot, creates Alert nodes for
      connections not previously alerted (avoids re-alerting on every check).
    - Always updates last_checked and snapshot_hash.

    Returns {"alerts": int, "changed": bool}.
    """
    new_hash, new_connections = await _snapshot(graph_db, watch["name"])
    old_hash = watch.get("snapshot_hash")

    changed = new_hash != old_hash
    alert_count = 0

    if changed:
        if old_hash is not None:
            # Diff: alert only on truly new nodes (not seen in any prior cycle)
            already = await _alerted_ids(graph_db, watch["id"])
            alert_count = await _create_alerts(graph_db, watch, new_connections, already)
        # First-run with old_hash=None: store baseline without alerting
        log.info(
            "Monitor: '%s' snapshot changed → %d new alert(s)", watch["name"], alert_count
        )

    await _mark_checked(graph_db, watch["id"], new_hash)
    return {"alerts": alert_count, "changed": changed}


# ── Alert retrieval ───────────────────────────────────────────────────────────

async def get_alerts(graph_db, limit: int = 50) -> list[dict]:
    """Return the most recent alerts, newest first."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (a:Alert)
            RETURN a { .* } AS a
            ORDER BY a.created_at DESC
            LIMIT $limit
            """,
            limit=limit,
        )
        return [dict(r["a"]) async for r in result]


async def mark_alerts_seen(graph_db, alert_ids: list[str]) -> int:
    """
    Mark specific alerts as seen. If alert_ids is empty, marks ALL unseen alerts.
    Returns the count of alerts updated.
    """
    async with graph_db.driver.session() as session:
        if alert_ids:
            result = await session.run(
                "MATCH (a:Alert) WHERE a.id IN $ids SET a.seen = true RETURN count(a) AS n",
                ids=alert_ids,
            )
        else:
            result = await session.run(
                "MATCH (a:Alert {seen: false}) SET a.seen = true RETURN count(a) AS n"
            )
        record = await result.single()
        return int(record["n"]) if record else 0


async def get_unseen_count(graph_db) -> int:
    """Return the count of unseen Alert nodes."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (a:Alert {seen: false}) RETURN count(a) AS n"
        )
        record = await result.single()
        return int(record["n"]) if record else 0

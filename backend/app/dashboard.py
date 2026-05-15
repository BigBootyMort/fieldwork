"""
Dashboard analytics — fast read-only queries that power the home screen.

All queries are designed to run in under 200 ms on a typical investigation
database (< 100 k nodes).  They are deliberately simple: no APOC procedures,
no full-table scans, just indexed label lookups and aggregations.

Public API
----------
get_dashboard_stats(graph_db)   → full dashboard payload
get_recent_activity(graph_db)   → last 20 created/modified nodes
get_top_connected(graph_db)     → 10 most-connected entities
get_entity_timeline(graph_db)   → daily creation counts, last 30 days
"""
import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("fieldwork.dashboard")

# Labels treated as "entities" (not meta-nodes)
_ENTITY_LABELS = [
    "Person", "Company", "Email", "Phone", "Username", "IP", "Domain",
    "URL", "Wallet", "Location", "Aircraft", "Breach", "Leak",
    "DarkWebMention", "TelegramChannel", "Account",
]


async def _run(session, cypher: str, **params):
    result = await session.run(cypher, **params)
    return await result.data()


async def get_dashboard_stats(graph_db) -> dict:
    """Return the complete dashboard payload in one call (parallel queries)."""

    async with graph_db.driver.session() as session:

        # Run all aggregation queries in parallel
        (
            node_rows,
            rel_rows,
            label_rows,
            case_rows,
            risk_rows,
            alert_rows,
        ) = await asyncio.gather(
            _run(session, "MATCH (n) WHERE n.id IS NOT NULL RETURN count(n) AS total"),
            _run(session, "MATCH ()-[r]->() RETURN count(r) AS total"),
            _run(session, """
                MATCH (n) WHERE n.id IS NOT NULL
                WITH labels(n) AS lbls
                UNWIND lbls AS lbl
                WHERE lbl IN $entity_labels
                RETURN lbl AS label, count(*) AS count
                ORDER BY count DESC
            """, entity_labels=_ENTITY_LABELS),
            _run(session, """
                MATCH (c:Case)
                RETURN coalesce(c.status, 'unknown') AS status, count(c) AS count
                ORDER BY count DESC
            """),
            _run(session, """
                MATCH (n) WHERE n.risk_level IS NOT NULL
                RETURN n.risk_level AS level, count(n) AS count
            """),
            _run(session, """
                MATCH (n:MonitorAlert) WHERE n.seen = false
                RETURN count(n) AS total
            """),
        )

        recent, top_connected = await asyncio.gather(
            get_recent_activity(graph_db),
            get_top_connected(graph_db),
        )

    total_nodes = node_rows[0]["total"] if node_rows else 0
    total_rels  = rel_rows[0]["total"]  if rel_rows  else 0

    # Entity counts
    entity_counts = {r["label"]: int(r["count"]) for r in label_rows}
    total_entities = sum(entity_counts.values())

    # Case stats
    case_stats = {r["status"]: int(r["count"]) for r in case_rows}
    total_cases = sum(case_stats.values())

    # Risk summary
    risk_summary = {r["level"]: int(r["count"]) for r in risk_rows}
    high_risk   = risk_summary.get("high", 0)
    medium_risk = risk_summary.get("medium", 0)

    # Unseen alerts
    unseen_alerts = alert_rows[0]["total"] if alert_rows else 0

    return {
        "totals": {
            "nodes":        total_nodes,
            "relationships": total_rels,
            "entities":     total_entities,
            "cases":        total_cases,
            "high_risk":    high_risk,
            "medium_risk":  medium_risk,
            "alerts":       unseen_alerts,
        },
        "entity_counts": entity_counts,
        "case_stats":    case_stats,
        "risk_summary":  risk_summary,
        "recent":        recent,
        "top_connected": top_connected,
    }


async def get_recent_activity(graph_db, limit: int = 20) -> list[dict]:
    """Return the most recently created/updated entity nodes."""
    async with graph_db.driver.session() as session:
        # Try with created_at first, fall back to id-ordering for nodes without timestamps
        rows = await _run(session, """
            MATCH (n) WHERE n.id IS NOT NULL
            WITH n,
                 labels(n) AS lbls,
                 coalesce(n.created_at, n.updated_at, '') AS ts,
                 coalesce(n.name, n.title, n.handle, n.address,
                          n.email, n.username, n.ip, n.url, n.id) AS display
            WHERE any(l IN lbls WHERE l IN $entity_labels)
            RETURN n.id AS id, lbls AS labels, display, ts, n.risk_level AS risk_level
            ORDER BY ts DESC
            LIMIT $limit
        """, entity_labels=_ENTITY_LABELS, limit=limit)

    out = []
    for r in rows:
        lbls  = r["labels"] or []
        label = next((l for l in lbls if l in _ENTITY_LABELS), lbls[0] if lbls else "Entity")
        out.append({
            "id":         r["id"],
            "label":      label,
            "display":    r["display"] or r["id"],
            "ts":         r["ts"] or "",
            "risk_level": r["risk_level"],
        })
    return out


async def get_top_connected(graph_db, limit: int = 10) -> list[dict]:
    """Return the most-connected entity nodes by relationship degree."""
    async with graph_db.driver.session() as session:
        rows = await _run(session, """
            MATCH (n)-[r]-()
            WHERE n.id IS NOT NULL
              AND any(l IN labels(n) WHERE l IN $entity_labels)
            WITH n, labels(n) AS lbls, count(r) AS degree
            WHERE degree > 0
            RETURN n.id AS id,
                   lbls AS labels,
                   coalesce(n.name, n.title, n.handle, n.address,
                            n.email, n.username, n.id) AS display,
                   degree,
                   n.risk_level AS risk_level,
                   n.risk_score AS risk_score
            ORDER BY degree DESC
            LIMIT $limit
        """, entity_labels=_ENTITY_LABELS, limit=limit)

    out = []
    for r in rows:
        lbls  = r["labels"] or []
        label = next((l for l in lbls if l in _ENTITY_LABELS), lbls[0] if lbls else "Entity")
        out.append({
            "id":         r["id"],
            "label":      label,
            "display":    r["display"] or r["id"],
            "degree":     int(r["degree"]),
            "risk_level": r["risk_level"],
            "risk_score": r["risk_score"],
        })
    return out


async def get_entity_timeline(graph_db, days: int = 30) -> list[dict]:
    """Return daily entity-creation counts for the last N days."""
    async with graph_db.driver.session() as session:
        rows = await _run(session, """
            MATCH (n) WHERE n.id IS NOT NULL AND n.created_at IS NOT NULL
              AND any(l IN labels(n) WHERE l IN $entity_labels)
            WITH date(datetime(n.created_at)) AS day, count(n) AS cnt
            ORDER BY day DESC
            LIMIT $days
            RETURN day, cnt
            ORDER BY day ASC
        """, entity_labels=_ENTITY_LABELS, days=days)

    return [{"date": str(r["day"]), "count": int(r["cnt"])} for r in rows]

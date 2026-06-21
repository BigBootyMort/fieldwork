"""
Phase 10 — Graph Visualization Engine.

Builds Cytoscape-compatible subgraph payloads:
  - subgraph_around()  : BFS from a known entity, returns nodes + edges
  - full_graph_sample(): random sample of the entire graph
  - expand_node()      : 1-hop neighbors of a single node (interactive drill-down)

Edge deduplication: uses frozenset(source, target, rel_type) so that
A→B and B→A with the same relationship type appear as one edge.
"""

import logging
from typing import Optional

log = logging.getLogger("fieldwork.graph_viz")

# ── Colour palette for Cytoscape nodes ───────────────────────────────────────
_LABEL_COLOR: dict[str, str] = {
    "Person":          "#c1440e",
    "Company":         "#0f6e56",
    "Email":           "#7c3aed",
    "Phone":           "#0369a1",
    "Username":        "#0891b2",
    "Account":         "#6d28d9",
    "IP":              "#b45309",
    "Domain":          "#047857",
    "Breach":          "#dc2626",
    "Leak":            "#b91c1c",
    "Wallet":          "#d97706",
    "Aircraft":        "#0284c7",
    "Flight":          "#0ea5e9",
    "DarkWebMention":  "#6b21a8",
    "TelegramChannel": "#0369a1",
    "TelegramPost":    "#075985",
    "Article":         "#4b5563",
    "Location":        "#166534",
    "WatchedSubject":  "#92400e",
    "Alert":           "#991b1b",
}
_DEFAULT_COLOR = "#374151"

_WEIGHT: dict[str, int] = {
    "Breach": 10, "Leak": 10, "Wallet": 9, "DarkWebMention": 8,
    "Account": 8, "Person": 8, "Email": 7, "Phone": 7, "Aircraft": 7,
    "TelegramPost": 7, "Company": 7, "Domain": 6, "IP": 6,
    "Username": 6, "TelegramChannel": 6, "Flight": 5, "Article": 5, "Location": 4,
}


def _node_size(degree: int) -> int:
    """Scale node size logarithmically between 22 and 72 px."""
    return max(22, min(72, 22 + int(degree ** 0.55) * 6))


def _fmt_node(node_id: str, label: str, props: dict, degree: int) -> dict:
    """Format a single node into the Cytoscape element envelope."""
    from pivot import _display  # reuse the display-value picker
    display = _display(props)[:40] or node_id[:40]
    return {
        "data": {
            "id":           node_id,
            "label":        label,
            "display_name": display,
            "degree":       degree,
            "weight":       _WEIGHT.get(label, 3),
            "color":        _LABEL_COLOR.get(label, _DEFAULT_COLOR),
            "size":         _node_size(degree),
            "props":        props,
        }
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def subgraph_around(
    graph_db,
    entity_id: str,
    label: Optional[str] = None,
    depth: int = 2,
    limit: int = 150,
) -> dict:
    """
    BFS from *entity_id* up to *depth* hops.
    Reuses pivot_from() for traversal, then fetches all edges between
    collected nodes in a single query.
    """
    from pivot import pivot_from

    depth = max(1, min(depth, 3))
    limit = max(10, min(limit, 300))

    data = await pivot_from(graph_db, entity_id, label=label, depth=depth, max_per_hop=limit)
    if not data["source"]:
        return {"nodes": [], "edges": [], "meta": {"center": entity_id, "depth": depth, "node_count": 0, "edge_count": 0}}

    src = data["source"]
    all_nodes: dict[str, dict] = {
        entity_id: _fmt_node(entity_id, src["label"], src["props"], src["degree"])
    }

    for hop in data["hops"]:
        for n in hop["nodes"]:
            nid = n["id"]
            if nid not in all_nodes and len(all_nodes) < limit:
                all_nodes[nid] = _fmt_node(nid, n["label"], n["props"], n["degree"])

    edges = await _fetch_edges(graph_db, list(all_nodes.keys()), limit * 4)

    return {
        "nodes": list(all_nodes.values()),
        "edges": edges,
        "meta": {
            "center":     entity_id,
            "depth":      depth,
            "node_count": len(all_nodes),
            "edge_count": len(edges),
        },
    }


async def full_graph_sample(graph_db, limit: int = 200) -> dict:
    """
    Random sample of up to *limit* nodes from the entire graph, plus the
    edges that connect them. Useful for a high-level overview.
    """
    limit = max(20, min(limit, 500))
    all_nodes: dict[str, dict] = {}

    async with graph_db.driver.session() as session:
        try:
            result = await session.run(
                """
                MATCH (n)
                WHERE n.id IS NOT NULL
                WITH n, labels(n)[0] AS lbl, rand() AS r
                ORDER BY r
                LIMIT $limit
                OPTIONAL MATCH (n)-[rdeg]-()
                WITH n, lbl, count(rdeg) AS degree
                RETURN n.id AS nid, lbl, n {.*} AS props, degree
                """,
                limit=limit,
            )
            async for r in result:
                nid = r["nid"]
                if nid:
                    all_nodes[nid] = _fmt_node(
                        nid,
                        r["lbl"] or "Unknown",
                        dict(r["props"]),
                        int(r["degree"] or 0),
                    )
        except Exception as e:
            log.warning("full_graph_sample error: %s", e)

    edges = await _fetch_edges(graph_db, list(all_nodes.keys()), limit * 3)

    return {
        "nodes": list(all_nodes.values()),
        "edges": edges,
        "meta": {"node_count": len(all_nodes), "edge_count": len(edges)},
    }


async def expand_node(
    graph_db,
    entity_id: str,
    label: Optional[str] = None,
) -> dict:
    """
    1-hop expansion from *entity_id*.
    Returns nodes + edges suitable for merging into an existing canvas.
    """
    from pivot import pivot_from

    data = await pivot_from(graph_db, entity_id, label=label, depth=1, max_per_hop=80)
    if not data["source"]:
        return {"nodes": [], "edges": []}

    src = data["source"]
    nodes: dict[str, dict] = {
        entity_id: _fmt_node(entity_id, src["label"], src["props"], src["degree"])
    }
    if data["hops"]:
        for n in data["hops"][0]["nodes"]:
            nid = n["id"]
            if nid not in nodes:
                nodes[nid] = _fmt_node(nid, n["label"], n["props"], n["degree"])

    edges = await _fetch_edges(graph_db, list(nodes.keys()), 300)

    return {"nodes": list(nodes.values()), "edges": edges}


# ── Shared edge fetcher ───────────────────────────────────────────────────────

async def _fetch_edges(graph_db, node_ids: list[str], limit: int) -> list[dict]:
    """
    Fetch all relationships between the given node IDs.
    Uses id(a) < id(b) to avoid returning each edge twice from the DB.
    Also deduplicates client-side on (source, target, rel_type).
    """
    if len(node_ids) < 2:
        return []

    edges: list[dict] = []
    seen: set[frozenset] = set()

    async with graph_db.driver.session() as session:
        try:
            result = await session.run(
                """
                MATCH (a)-[r]-(b)
                WHERE a.id IN $ids AND b.id IN $ids AND id(a) < id(b)
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, id(r) AS rid
                LIMIT $limit
                """,
                ids=node_ids,
                limit=limit,
            )
            async for r in result:
                key = frozenset([r["source"], r["target"], r["rel_type"]])
                if key in seen:
                    continue
                seen.add(key)
                edges.append({
                    "data": {
                        "id":       f"e_{r['rid']}",
                        "source":   r["source"],
                        "target":   r["target"],
                        "rel_type": r["rel_type"],
                    }
                })
        except Exception as e:
            log.warning("_fetch_edges error: %s", e)

    return edges

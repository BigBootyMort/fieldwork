"""
Phase 8 — Entity Pivot Engine.

Given any node in the graph, surfaces all connected entities ranked by
investigative relevance. Supports:

  - Universal entity search across all node types
  - Single-node summary (properties + degree)
  - Multi-hop BFS pivot with weighted scoring
  - Actionable next-step suggestions based on neighbor composition

Scoring: label weight (Breach > Wallet > Account > Person > …) × connection
degree. Higher score surfaces first in results.
"""
import logging
import re
from typing import Optional

log = logging.getLogger("fieldwork.pivot")

# ── Label weights (higher = more investigatively valuable) ────────────────────
_WEIGHT: dict[str, int] = {
    "Breach":          10,
    "Leak":            10,
    "Wallet":           9,
    "DarkWebMention":   8,
    "Account":          8,
    "Person":           8,
    "Email":            7,
    "Phone":            7,
    "Aircraft":         7,
    "TelegramPost":     7,
    "Company":          7,
    "Domain":           6,
    "IP":               6,
    "Username":         6,
    "TelegramChannel":  6,
    "Flight":           5,
    "Article":          5,
    "Location":         4,
}
_DEFAULT_WEIGHT = 3

# Labels that are valid to interpolate into Cypher (Neo4j: alphanum + _)
_VALID_LABELS: frozenset[str] = frozenset(
    list(_WEIGHT.keys()) + ["WatchedSubject", "Alert", "Unknown"]
)


def _safe_label(label: str) -> str:
    """Validate a label before Cypher f-string interpolation."""
    if label in _VALID_LABELS:
        return label
    # Accept any string that looks like a safe label (alphanumeric + _)
    if label and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", label):
        return label
    log.warning("Rejecting unsafe label: %r", label)
    return "Unknown"


# Search targets: (label, search_property, display_property)
_SEARCH_TARGETS: list[tuple[str, str, str]] = [
    ("Person",          "name",         "name"),
    ("Company",         "name",         "name"),
    ("Email",           "address",      "address"),
    ("Domain",          "name",         "name"),
    ("IP",              "address",      "address"),
    ("Username",        "handle",       "handle"),
    ("Account",         "url",          "url"),
    ("Wallet",          "id",           "id"),
    ("Aircraft",        "registration", "registration"),
    ("TelegramChannel", "name",         "name"),
    ("Breach",          "name",         "name"),
    ("Article",         "title",        "title"),
    ("DarkWebMention",  "query",        "query"),
]


def _display(props: dict) -> str:
    """Pick the most human-readable value from a node's property map."""
    for key in ("name", "address", "handle", "title", "registration", "url", "query", "id"):
        v = props.get(key)
        if v:
            return str(v)[:200]
    return ""


# ── Universal entity search ───────────────────────────────────────────────────

async def search_all_entities(
    graph_db, query: str, limit: int = 30
) -> list[dict]:
    """
    Case-insensitive substring search across all major node labels.
    Returns up to *limit* results sorted by label weight then display name.
    """
    q = query.strip()
    if not q:
        return []

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()   # (label, id) — avoids cross-label dupes

    async with graph_db.driver.session() as session:
        for label, search_prop, display_prop in _SEARCH_TARGETS:
            lbl = _safe_label(label)
            try:
                cypher = f"""
                    MATCH (n:{lbl})
                    WHERE toLower(toString(n.{search_prop})) CONTAINS toLower($q)
                    RETURN
                        n.id               AS nid,
                        labels(n)[0]       AS nlabel,
                        n.{display_prop}   AS display_name,
                        n {{ .* }}         AS props
                    LIMIT $limit
                """
                result = await session.run(cypher, q=q, limit=limit)
                async for r in result:
                    nid    = r["nid"]
                    nlabel = r["nlabel"] or lbl
                    key    = (nlabel, nid)
                    if not nid or key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "id":           nid,
                        "label":        nlabel,
                        "display_name": r["display_name"] or nid,
                        "weight":       _WEIGHT.get(nlabel, _DEFAULT_WEIGHT),
                        "props":        dict(r["props"]),
                    })
            except Exception as e:
                log.debug("Search error (label=%s): %s", label, e)

    results.sort(key=lambda x: (-x["weight"], str(x.get("display_name") or "").lower()))
    return results[:limit]


# ── Entity summary ────────────────────────────────────────────────────────────

async def entity_summary(
    graph_db, entity_id: str, label: Optional[str] = None
) -> Optional[dict]:
    """
    Fetch a node's properties, label, and total connection degree.
    If *label* is omitted, probes labels in weight order until a match is found.
    """
    labels_to_try: list[str] = (
        [label] if label else list(_WEIGHT.keys()) + ["WatchedSubject", "Alert"]
    )

    async with graph_db.driver.session() as session:
        for lbl in labels_to_try:
            safe = _safe_label(lbl)
            try:
                result = await session.run(
                    f"""
                    MATCH (n:{safe} {{id: $id}})
                    OPTIONAL MATCH (n)-[r]-()
                    RETURN
                        n {{ .* }}   AS props,
                        labels(n)[0] AS label,
                        count(r)     AS degree
                    LIMIT 1
                    """,
                    id=entity_id,
                )
                record = await result.single()
                if record:
                    props = dict(record["props"])
                    nlabel = record["label"] or lbl
                    return {
                        "id":           entity_id,
                        "label":        nlabel,
                        "degree":       int(record["degree"] or 0),
                        "display_name": _display(props),
                        "weight":       _WEIGHT.get(nlabel, _DEFAULT_WEIGHT),
                        "props":        props,
                    }
            except Exception as e:
                log.debug("Summary error (label=%s, id=%s): %s", lbl, entity_id, e)

    return None


# ── BFS pivot ─────────────────────────────────────────────────────────────────

async def pivot_from(
    graph_db,
    entity_id: str,
    label: Optional[str] = None,
    depth: int = 1,
    max_per_hop: int = 50,
) -> dict:
    """
    BFS pivot starting from *entity_id*.

    For each hop, fetches all direct neighbors of the current frontier,
    scores and sorts them, then sets the frontier to the top-N for the
    next hop (if depth > 1).

    Returns::

        {
          "source": {id, label, display_name, degree, weight, props},
          "hops": [
            {"hop": 1, "nodes": [...], "total": N},
            ...
          ]
        }

    Each node in *hops* contains:
      id, label, display_name, rel_types, weight, degree, props, from_id.
    """
    source = await entity_summary(graph_db, entity_id, label)
    if not source:
        return {"source": None, "hops": []}

    visited: set[str]             = {entity_id}
    hops:    list[dict]           = []
    frontier: list[tuple[str, str]] = [(entity_id, source["label"])]

    async with graph_db.driver.session() as session:
        for hop_num in range(1, depth + 1):
            hop_nodes: list[dict] = []
            next_frontier: list[tuple[str, str]] = []

            for fid, flabel in frontier:
                safe_fl = _safe_label(flabel)
                try:
                    # Single query: collect all rel types per neighbor + count its degree
                    result = await session.run(
                        f"""
                        MATCH (src:{safe_fl} {{id: $id}})-[r]-(n)
                        WHERE n.id IS NOT NULL
                        WITH n, collect(DISTINCT type(r)) AS rel_types
                        MATCH (n)-[rdeg]-()
                        RETURN
                            n.id             AS nid,
                            labels(n)[0]     AS nlabel,
                            rel_types,
                            n {{ .* }}       AS props,
                            count(rdeg)      AS degree
                        ORDER BY degree DESC
                        LIMIT $limit
                        """,
                        id=fid, limit=max_per_hop,
                    )
                    async for r in result:
                        nid = r["nid"]
                        if not nid or nid in visited:
                            continue
                        visited.add(nid)

                        nlabel = r["nlabel"] or "Unknown"
                        props  = dict(r["props"])
                        node   = {
                            "id":           nid,
                            "label":        nlabel,
                            "display_name": _display(props),
                            "rel_types":    list(r["rel_types"]),
                            "weight":       _WEIGHT.get(nlabel, _DEFAULT_WEIGHT),
                            "degree":       int(r["degree"] or 0),
                            "props":        props,
                            "from_id":      fid,
                        }
                        hop_nodes.append(node)
                        if hop_num < depth:
                            next_frontier.append((nid, nlabel))
                except Exception as e:
                    log.debug(
                        "Pivot error from %s (%s) hop %d: %s", fid, flabel, hop_num, e
                    )

            hop_nodes.sort(key=lambda x: (-x["weight"], -x["degree"]))
            hops.append({
                "hop":   hop_num,
                "nodes": hop_nodes[:max_per_hop],
                "total": len(hop_nodes),
            })
            # Cap frontier width to prevent exponential blow-up on deep pivots
            frontier = next_frontier[:15]

    return {"source": source, "hops": hops}


# ── Pivot suggestions ─────────────────────────────────────────────────────────

# Rules that fire when a particular label IS present in the 1-hop neighborhood
_PRESENCE_RULES: list[tuple[str, str, str]] = [
    # (neighbor_label,   action,                               hint)
    ("Breach",          "🔓 Expand breach data",              "review linked breaches for credential and password exposure"),
    ("Wallet",          "💰 Trace crypto wallets",            "look up linked wallets via Arkham Intelligence"),
    ("DarkWebMention",  "🌑 Investigate dark web results",    "review Ahmia mentions — search for related queries"),
    ("Account",         "👤 Explore social accounts",         "open account URLs or run a deeper Maigret username scan"),
    ("Person",          "🔗 Profile linked persons",          "run a full crawl on each connected individual"),
    ("Email",           "📧 Pivot via email addresses",       "check HIBP or search the graph for other owners"),
    ("Aircraft",        "✈ Check flight history",             "fetch OpenSky flights for all linked aircraft"),
    ("TelegramChannel", "📡 Read Telegram channel",           "scrape the linked channel for further entity mentions"),
    ("Company",         "🏢 Research linked companies",       "search OpenCorporates, Aleph, and SEC EDGAR"),
    ("Domain",          "🌐 Enrich linked domains",           "run RDAP, VirusTotal, or Wayback Machine"),
    ("IP",              "🔌 Enrich linked IPs",               "run Shodan or VirusTotal for host intelligence"),
    ("Article",         "📰 Read news articles",              "full-text articles may contain additional entity mentions"),
]

# Rules that fire when a label IS present but a related label IS NOT
_MISSING_RULES: list[tuple[str, str, str, str]] = [
    # (present,  missing,          action,                       hint)
    ("Email",  "Breach",          "🔓 Check emails for breaches",
               "no breach data yet — run HIBP on linked email addresses"),
    ("Domain", "DarkWebMention",  "🌑 Search dark web for linked domain",
               "no dark web results yet — try an Ahmia search"),
    ("IP",     "Domain",          "🌐 Reverse-DNS these IPs",
               "look up hosted domains via VirusTotal passive DNS"),
    ("Person", "Account",         "👤 Find social accounts for linked persons",
               "no social account data — run a Maigret username scan"),
]


async def pivot_suggestions(
    graph_db, entity_id: str, label: Optional[str] = None
) -> list[dict]:
    """
    Analyse 1-hop neighbors and return ranked actionable next steps.
    """
    data = await pivot_from(
        graph_db, entity_id, label=label, depth=1, max_per_hop=200
    )
    if not data["hops"]:
        return []

    neighbor_labels: dict[str, int] = {}
    for node in data["hops"][0]["nodes"]:
        lbl = node["label"]
        neighbor_labels[lbl] = neighbor_labels.get(lbl, 0) + 1

    suggestions: list[dict] = []

    for nlabel, action, hint in _PRESENCE_RULES:
        if nlabel in neighbor_labels:
            suggestions.append({
                "action": action,
                "hint":   hint,
                "count":  neighbor_labels[nlabel],
                "label":  nlabel,
                "type":   "expand",
            })

    for present, missing, action, hint in _MISSING_RULES:
        if present in neighbor_labels and missing not in neighbor_labels:
            suggestions.append({
                "action": action,
                "hint":   hint,
                "count":  neighbor_labels[present],
                "label":  present,
                "type":   "enrich",
            })

    # Deduplicate on action string
    seen_actions: set[str] = set()
    deduped: list[dict] = []
    for s in suggestions:
        if s["action"] not in seen_actions:
            seen_actions.add(s["action"])
            deduped.append(s)

    deduped.sort(key=lambda x: -x["count"])
    return deduped

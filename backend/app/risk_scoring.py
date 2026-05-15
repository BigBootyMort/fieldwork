"""
Risk Scoring — compute a 0-100 risk score for entity nodes.

Signals used (additive, capped at 100):
  +40  VirusTotal malicious votes (vt_malicious > 0)
  +20  VirusTotal suspicious votes only (vt_suspicious > 0, no malicious)
  +25  Shodan — sensitive ports open (22,23,3389,445,5900,8080)
  +35  Connected DarkWebMention nodes
  +25  Connected Breach / Leak nodes
  +35  HIBP breach count > 0  (hibp_count property)
  +15  Shodan — any open ports (low-signal bump)

Risk levels:
  high   ≥ 60
  medium ≥ 25
  low    < 25

Public API
----------
score_entity(graph_db, entity_id)       → score dict for one entity
score_all_entities(graph_db, labels)    → bulk-score all entity nodes
get_high_risk_entities(graph_db, limit) → top N high-risk nodes
"""
import asyncio
import logging
from typing import Optional

log = logging.getLogger("fieldwork.risk_scoring")

_SENSITIVE_PORTS = {22, 23, 25, 53, 80, 110, 143, 443, 445, 3306, 3389,
                    5432, 5900, 6379, 8080, 8443, 8888, 27017}

_ENTITY_LABELS = [
    "Person", "Company", "Email", "Phone", "Username", "IP", "Domain",
    "URL", "Wallet", "Location", "Aircraft", "Breach", "Leak",
    "DarkWebMention", "TelegramChannel", "Account",
]


async def _run(session, cypher: str, **params):
    result = await session.run(cypher, **params)
    return await result.data()


def _compute_score(props: dict, dark_web: int, breaches: int) -> tuple[int, str, list[str]]:
    """Pure function — returns (score, level, factors)."""
    score = 0
    factors: list[str] = []

    # AbuseIPDB confidence score (0-100)
    abuse_conf = props.get("abuse_confidence") or 0
    if abuse_conf and int(abuse_conf) >= 75:
        score += 45
        cats = props.get("abuse_categories") or []
        cat_str = ", ".join(cats[:3]) if cats else ""
        factors.append(f"AbuseIPDB: {abuse_conf}% confidence ({cat_str})" if cat_str else f"AbuseIPDB: {abuse_conf}% confidence")
    elif abuse_conf and int(abuse_conf) >= 25:
        score += 25
        factors.append(f"AbuseIPDB: {abuse_conf}% confidence (suspicious)")

    # Tor exit node
    if props.get("is_tor"):
        score += 20
        factors.append("Tor exit node")

    # VirusTotal
    vt_malicious  = props.get("vt_malicious")  or 0
    vt_suspicious = props.get("vt_suspicious") or 0
    if vt_malicious and int(vt_malicious) > 0:
        score += 40
        factors.append(f"VirusTotal: {vt_malicious} malicious votes")
    elif vt_suspicious and int(vt_suspicious) > 0:
        score += 20
        factors.append(f"VirusTotal: {vt_suspicious} suspicious votes")

    # Shodan open ports
    ports_raw = props.get("shodan_ports") or []
    if isinstance(ports_raw, str):
        try:
            import json
            ports_raw = json.loads(ports_raw)
        except Exception:
            ports_raw = []
    ports = set(int(p) for p in ports_raw if str(p).isdigit())
    sensitive = ports & _SENSITIVE_PORTS
    if sensitive:
        score += 25
        factors.append(f"Shodan: sensitive ports open ({', '.join(str(p) for p in sorted(sensitive))})")
    elif ports:
        score += 15
        factors.append(f"Shodan: {len(ports)} open ports")

    # Dark web mentions
    if dark_web > 0:
        score += 35
        factors.append(f"Dark web mentions: {dark_web}")

    # Breach / Leak connections
    if breaches > 0:
        score += 25
        factors.append(f"Breach/Leak connections: {breaches}")

    # HIBP
    hibp = props.get("hibp_count") or 0
    if hibp and int(hibp) > 0:
        score += 35
        factors.append(f"HIBP: found in {hibp} breach(es)")

    score = min(score, 100)

    if score >= 60:
        level = "high"
    elif score >= 25:
        level = "medium"
    else:
        level = "low"

    return score, level, factors


async def score_entity(graph_db, entity_id: str) -> dict:
    """
    Score a single entity by ID.
    Writes risk_score / risk_level / risk_factors back to the node.
    Returns the score dict.
    """
    async with graph_db.driver.session() as session:
        # Fetch node properties
        node_rows = await _run(session, """
            MATCH (n) WHERE n.id = $id
            RETURN properties(n) AS props, labels(n) AS lbls
        """, id=entity_id)

        if not node_rows:
            return {"error": "entity not found", "id": entity_id}

        props = node_rows[0]["props"] or {}
        lbls  = node_rows[0]["lbls"]  or []

        # Fetch dark-web connections
        dw_rows = await _run(session, """
            MATCH (n)-[:MENTIONED_IN|HAS_MENTION|LINKED_TO|RELATED_TO*1..2]-(m:DarkWebMention)
            WHERE n.id = $id
            RETURN count(DISTINCT m) AS cnt
        """, id=entity_id)
        dark_web = dw_rows[0]["cnt"] if dw_rows else 0

        # Fetch breach/leak connections
        br_rows = await _run(session, """
            MATCH (n)-[*1..2]-(m)
            WHERE n.id = $id AND (m:Breach OR m:Leak)
            RETURN count(DISTINCT m) AS cnt
        """, id=entity_id)
        breaches = br_rows[0]["cnt"] if br_rows else 0

    score, level, factors = _compute_score(props, dark_web, breaches)

    # Persist to node
    async with graph_db.driver.session() as session:
        import json
        await session.run("""
            MATCH (n) WHERE n.id = $id
            SET n.risk_score   = $score,
                n.risk_level   = $level,
                n.risk_factors = $factors
        """, id=entity_id, score=score, level=level, factors=json.dumps(factors))

    label = next((l for l in lbls if l in _ENTITY_LABELS), lbls[0] if lbls else "Entity")
    return {
        "id":           entity_id,
        "label":        label,
        "risk_score":   score,
        "risk_level":   level,
        "risk_factors": factors,
    }


async def score_all_entities(graph_db, labels: Optional[list[str]] = None) -> dict:
    """
    Bulk-score every entity node that lacks an up-to-date risk_score.
    Runs scoring concurrently in batches of 20.
    Returns summary stats.
    """
    target_labels = labels or _ENTITY_LABELS

    async with graph_db.driver.session() as session:
        rows = await _run(session, """
            MATCH (n) WHERE n.id IS NOT NULL
              AND any(l IN labels(n) WHERE l IN $lbls)
            RETURN n.id AS id
        """, lbls=target_labels)

    ids = [r["id"] for r in rows]
    log.info("Risk scoring %d entities …", len(ids))

    batch_size = 20
    results = {"scored": 0, "high": 0, "medium": 0, "low": 0, "errors": 0}

    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        scored = await asyncio.gather(
            *[score_entity(graph_db, eid) for eid in batch],
            return_exceptions=True,
        )
        for r in scored:
            if isinstance(r, Exception):
                results["errors"] += 1
            elif "error" not in r:
                results["scored"] += 1
                results[r["risk_level"]] += 1

    return results


async def get_high_risk_entities(graph_db, limit: int = 20) -> list[dict]:
    """Return the N highest-scored entities."""
    async with graph_db.driver.session() as session:
        rows = await _run(session, """
            MATCH (n) WHERE n.risk_score IS NOT NULL AND n.id IS NOT NULL
              AND any(l IN labels(n) WHERE l IN $lbls)
            WITH n, labels(n) AS lbls
            RETURN n.id AS id,
                   lbls AS labels,
                   coalesce(n.name, n.title, n.handle, n.email,
                            n.username, n.address, n.id) AS display,
                   n.risk_score   AS risk_score,
                   n.risk_level   AS risk_level,
                   n.risk_factors AS risk_factors
            ORDER BY n.risk_score DESC
            LIMIT $limit
        """, lbls=_ENTITY_LABELS, limit=limit)

    import json
    out = []
    for r in rows:
        lbls  = r["labels"] or []
        label = next((l for l in lbls if l in _ENTITY_LABELS), lbls[0] if lbls else "Entity")
        raw_factors = r["risk_factors"]
        if isinstance(raw_factors, str):
            try:
                factors = json.loads(raw_factors)
            except Exception:
                factors = [raw_factors]
        else:
            factors = raw_factors or []
        out.append({
            "id":           r["id"],
            "label":        label,
            "display":      r["display"] or r["id"],
            "risk_score":   int(r["risk_score"]),
            "risk_level":   r["risk_level"],
            "risk_factors": factors,
        })
    return out

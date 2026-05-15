"""
Graph quality — duplicate detection, node merge, and health stats (Phase 5)

Public API
----------
run_duplicate_detection(graph_db)         → {"created": int, "skipped": int}
get_duplicate_candidates(graph_db)        → list[dict]
merge_persons(graph_db, keep_id, del_id)  → dict
dismiss_duplicate(graph_db, id_a, id_b)  → None
get_graph_health(graph_db)               → dict

Duplicate scoring
-----------------
Candidates are found two ways, then combined and de-duped:

  1. Name similarity   — Jaro-Winkler ≥ 0.88 via APOC
  2. Shared attribute  — same Email / Account / Company neighbour

Confidence ∈ [0.0, 1.0] is written onto the POSSIBLE_DUPLICATE relationship.
NER-sourced nodes get a small penalty because they are noisier.

Merge
-----
Uses APOC apoc.refactor.mergeNodes so all relationships from the deleted node
are re-pointed to the surviving node automatically. The caller chooses which
node to keep; typically prefer the node with more connections or a trusted
source (SEC, GitHub) over one created by NER.
"""
import logging
from typing import Any

log = logging.getLogger("fieldwork.dedup")

# ── Confidence weights ────────────────────────────────────────────────────────
_W_NAME_HIGH  = 0.90   # Jaro-Winkler ≥ 0.95
_W_NAME_MED   = 0.72   # Jaro-Winkler ≥ 0.88
_W_SHARED_EMAIL   = 0.25
_W_SHARED_ACCOUNT = 0.20
_W_SHARED_COMPANY = 0.10
_W_NER_PENALTY    = 0.10   # subtracted when both nodes are NER-sourced


# ── Detection ─────────────────────────────────────────────────────────────────

async def run_duplicate_detection(graph_db) -> dict:
    """
    Scan the Person graph for likely duplicates.
    Writes POSSIBLE_DUPLICATE relationships with a confidence score.
    Skips pairs already marked POSSIBLE_DUPLICATE or CONFIRMED_DISTINCT.
    Returns {"created": N, "skipped": M}.
    """
    candidates = await _find_name_candidates(graph_db)
    candidates += await _find_attribute_candidates(graph_db)
    candidates  = _merge_candidate_scores(candidates)

    created = skipped = 0
    async with graph_db.driver.session() as session:
        for c in candidates:
            # Check if relationship already exists in either direction
            exists = await session.run(
                "MATCH (a:Person {id:$id_a}), (b:Person {id:$id_b}) "
                "RETURN EXISTS((a)-[:POSSIBLE_DUPLICATE|CONFIRMED_DISTINCT]-(b)) AS exists",
                id_a=c["id_a"], id_b=c["id_b"],
            )
            record = await exists.single()
            if record and record["exists"]:
                skipped += 1
                continue

            await session.run(
                "MATCH (a:Person {id:$id_a}), (b:Person {id:$id_b}) "
                "MERGE (a)-[r:POSSIBLE_DUPLICATE]-(b) "
                "ON CREATE SET r.confidence = $conf, "
                "              r.reasons    = $reasons, "
                "              r.created    = datetime()",
                id_a=c["id_a"], id_b=c["id_b"],
                conf=c["confidence"], reasons=c["reasons"],
            )
            created += 1

    log.info("Dedup scan: %d new candidates, %d already known", created, skipped)
    return {"created": created, "skipped": skipped}


async def _find_name_candidates(graph_db) -> list[dict]:
    """Use APOC Jaro-Winkler to find name-similar Person pairs."""
    results = []
    async with graph_db.driver.session() as session:
        try:
            cursor = await session.run(
                """
                MATCH (a:Person), (b:Person)
                WHERE a.id < b.id
                  AND a.name IS NOT NULL AND b.name IS NOT NULL
                  AND size(a.name) > 2  AND size(b.name) > 2
                WITH a, b,
                     apoc.text.jaroWinklerDistance(
                         toLower(a.name), toLower(b.name)
                     ) AS sim
                WHERE sim >= 0.88
                RETURN a.id AS id_a, a.name AS name_a, a.source AS src_a,
                       b.id AS id_b, b.name AS name_b, b.source AS src_b,
                       sim
                ORDER BY sim DESC
                LIMIT 300
                """
            )
            async for row in cursor:
                sim = row["sim"]
                conf = _W_NAME_HIGH if sim >= 0.95 else _W_NAME_MED
                both_ner = (row["src_a"] == "ner" and row["src_b"] == "ner")
                if both_ner:
                    conf = max(0.0, conf - _W_NER_PENALTY)
                results.append({
                    "id_a":  row["id_a"],
                    "name_a": row["name_a"],
                    "id_b":  row["id_b"],
                    "name_b": row["name_b"],
                    "confidence": round(conf, 3),
                    "reasons": [f"name_similarity:{sim:.3f}"],
                })
        except Exception as exc:
            # APOC may not be available — fall back gracefully
            log.warning("Name-similarity scan failed (APOC available?): %s", exc)
    return results


async def _find_attribute_candidates(graph_db) -> list[dict]:
    """Find Person pairs that share an Email, Account, or Company neighbour."""
    results = []
    queries = [
        # Shared email
        (
            """
            MATCH (a:Person)-[:HAS_EMAIL|FEATURED_IN|USES_EMAIL]-(e:Email)
                  -[:HAS_EMAIL|FEATURED_IN|USES_EMAIL]-(b:Person)
            WHERE a.id < b.id
            RETURN a.id AS id_a, a.name AS name_a,
                   b.id AS id_b, b.name AS name_b,
                   'shared_email' AS reason
            LIMIT 100
            """,
            _W_SHARED_EMAIL,
        ),
        # Shared claimed account (same URL)
        (
            """
            MATCH (a:Person)-[:HAS_ACCOUNT]-(acc:Account)-[:HAS_ACCOUNT]-(b:Person)
            WHERE a.id < b.id
            RETURN a.id AS id_a, a.name AS name_a,
                   b.id AS id_b, b.name AS name_b,
                   'shared_account' AS reason
            LIMIT 100
            """,
            _W_SHARED_ACCOUNT,
        ),
        # Shared company (strong signal when combined with name similarity)
        (
            """
            MATCH (a:Person)-[:WORKS_AT|BOARD_MEMBER_OF|OFFICER_OF]-(c:Company)
                  -[:WORKS_AT|BOARD_MEMBER_OF|OFFICER_OF]-(b:Person)
            WHERE a.id < b.id
            RETURN a.id AS id_a, a.name AS name_a,
                   b.id AS id_b, b.name AS name_b,
                   'shared_company' AS reason
            LIMIT 200
            """,
            _W_SHARED_COMPANY,
        ),
    ]

    async with graph_db.driver.session() as session:
        for query, weight in queries:
            try:
                cursor = await session.run(query)
                async for row in cursor:
                    results.append({
                        "id_a":  row["id_a"],
                        "name_a": row["name_a"],
                        "id_b":  row["id_b"],
                        "name_b": row["name_b"],
                        "confidence": weight,
                        "reasons": [row["reason"]],
                    })
            except Exception as exc:
                log.warning("Attribute dedup query failed: %s", exc)

    return results


def _merge_candidate_scores(candidates: list[dict]) -> list[dict]:
    """
    Combine duplicate entries for the same pair (from name + attribute passes).
    Confidence scores are summed and capped at 0.99.
    """
    merged: dict[tuple, dict] = {}
    for c in candidates:
        key = (min(c["id_a"], c["id_b"]), max(c["id_a"], c["id_b"]))
        if key in merged:
            merged[key]["confidence"] = min(
                0.99, merged[key]["confidence"] + c["confidence"]
            )
            merged[key]["reasons"] = list(
                dict.fromkeys(merged[key]["reasons"] + c["reasons"])
            )
        else:
            merged[key] = dict(c)
            merged[key]["id_a"], merged[key]["id_b"] = key

    # Only surface pairs with meaningful combined confidence
    return [v for v in merged.values() if v["confidence"] >= 0.50]


# ── Retrieval ─────────────────────────────────────────────────────────────────

async def get_duplicate_candidates(graph_db) -> list[dict]:
    """
    Return all open POSSIBLE_DUPLICATE pairs, enriched with connection counts
    so the UI can show which node is "heavier" (more data attached).
    """
    results = []
    async with graph_db.driver.session() as session:
        cursor = await session.run(
            """
            MATCH (a:Person)-[r:POSSIBLE_DUPLICATE]-(b:Person)
            WHERE a.id < b.id
            WITH a, b, r
            OPTIONAL MATCH (a)-[ra]-()
            WITH a, b, r, count(ra) AS conn_a
            OPTIONAL MATCH (b)-[rb]-()
            WITH a, b, r, conn_a, count(rb) AS conn_b
            RETURN
                a.id     AS id_a,  a.name   AS name_a,
                a.source AS src_a, conn_a,
                b.id     AS id_b,  b.name   AS name_b,
                b.source AS src_b, conn_b,
                r.confidence AS confidence,
                r.reasons    AS reasons
            ORDER BY r.confidence DESC
            LIMIT 100
            """
        )
        async for row in cursor:
            results.append({
                "id_a":        row["id_a"],
                "name_a":      row["name_a"],
                "source_a":    row["src_a"] or "unknown",
                "connections_a": row["conn_a"] or 0,
                "id_b":        row["id_b"],
                "name_b":      row["name_b"],
                "source_b":    row["src_b"] or "unknown",
                "connections_b": row["conn_b"] or 0,
                "confidence":  row["confidence"],
                "reasons":     row["reasons"] or [],
            })
    return results


# ── Merge ─────────────────────────────────────────────────────────────────────

async def merge_persons(graph_db, keep_id: str, delete_id: str) -> dict:
    """
    Merge two Person nodes using APOC refactor.
    All relationships from *delete_id* are re-pointed to *keep_id*.
    The *delete_id* node is removed.
    Returns the surviving node's properties.
    """
    async with graph_db.driver.session() as session:
        # First remove any dedup relationship between them so APOC doesn't
        # try to merge a self-relationship
        await session.run(
            "MATCH (a:Person {id:$kid})-[r:POSSIBLE_DUPLICATE|CONFIRMED_DISTINCT]-(b:Person {id:$did}) "
            "DELETE r",
            kid=keep_id, did=delete_id,
        )

        result = await session.run(
            """
            MATCH (keep:Person {id: $keep_id}), (del:Person {id: $del_id})
            CALL apoc.refactor.mergeNodes([keep, del], {
                properties: 'discard',
                mergeRels:  true
            }) YIELD node
            RETURN node.id AS id, node.name AS name, node.source AS source
            """,
            keep_id=keep_id, del_id=delete_id,
        )
        record = await result.single()
        if not record:
            raise ValueError(f"Merge failed — one or both nodes not found: {keep_id}, {delete_id}")

        log.info("Merged %s → %s (deleted %s)", delete_id, keep_id, delete_id)
        return {"id": record["id"], "name": record["name"], "source": record["source"]}


# ── Dismiss ───────────────────────────────────────────────────────────────────

async def dismiss_duplicate(graph_db, id_a: str, id_b: str) -> None:
    """
    Mark a pair as confirmed distinct — they will never surface again as candidates.
    Removes POSSIBLE_DUPLICATE and creates CONFIRMED_DISTINCT in its place.
    """
    async with graph_db.driver.session() as session:
        await session.run(
            "MATCH (a:Person {id:$id_a}), (b:Person {id:$id_b}) "
            "OPTIONAL MATCH (a)-[r:POSSIBLE_DUPLICATE]-(b) DELETE r "
            "WITH a, b "
            "MERGE (a)-[:CONFIRMED_DISTINCT]-(b)",
            id_a=id_a, id_b=id_b,
        )
    log.info("Dismissed duplicate pair: %s <-> %s", id_a, id_b)


# ── Graph health ──────────────────────────────────────────────────────────────

async def get_graph_health(graph_db) -> dict:
    """
    Return a health snapshot of the graph:
      - node counts by label
      - relationship counts by type
      - pending duplicate pairs
      - orphan Person nodes (no relationships at all)
      - NER-sourced node counts (candidates for resolver pass)
    """
    async with graph_db.driver.session() as session:

        # Node counts by label
        node_cursor = await session.run(
            "MATCH (n) UNWIND labels(n) AS lbl "
            "RETURN lbl AS label, count(n) AS count ORDER BY count DESC"
        )
        node_counts: dict[str, int] = {}
        async for row in node_cursor:
            node_counts[row["label"]] = row["count"]

        # Relationship counts by type
        rel_cursor = await session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY count DESC"
        )
        rel_counts: dict[str, int] = {}
        async for row in rel_cursor:
            rel_counts[row["type"]] = row["count"]

        # Pending duplicates
        dup_result = await session.run(
            "MATCH ()-[r:POSSIBLE_DUPLICATE]-() RETURN count(r)/2 AS n"
        )
        dup_record = await dup_result.single()
        pending_dupes = dup_record["n"] if dup_record else 0

        # Orphan persons (no relationships whatsoever)
        orphan_result = await session.run(
            "MATCH (p:Person) WHERE NOT (p)--() RETURN count(p) AS n"
        )
        orphan_record = await orphan_result.single()
        orphan_count = orphan_record["n"] if orphan_record else 0

        # NER-sourced nodes (noisier, resolver targets)
        ner_result = await session.run(
            "MATCH (p:Person {source:'ner'}) RETURN count(p) AS n"
        )
        ner_record = await ner_result.single()
        ner_persons = ner_record["n"] if ner_record else 0

    total_nodes = sum(node_counts.values())
    total_rels  = sum(rel_counts.values())

    return {
        "total_nodes":        total_nodes,
        "total_relationships": total_rels,
        "pending_duplicates": pending_dupes,
        "orphan_persons":     orphan_count,
        "ner_sourced_persons": ner_persons,
        "nodes_by_label":     node_counts,
        "rels_by_type":       rel_counts,
    }

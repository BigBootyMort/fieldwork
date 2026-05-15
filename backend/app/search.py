"""
Phase 18 — Global full-text search.

Uses a single multi-label, multi-property Lucene index in Neo4j to power a
unified search across every node type in the graph.
"""
import logging
import re

log = logging.getLogger("fieldwork.search")

# ── Index definition ────────────────────────────────────────────────────────

INDEX_NAME = "fieldwork_fts"

# Labels and properties included in the index.
# Keep this in sync with neo4j/init.cypher.
_LABELS = [
    "Person", "Company", "Location",
    "Email", "Phone", "Username", "Account",
    "IP", "Domain", "Breach", "Leak", "Wallet",
    "Article", "Document",
    "Aircraft", "Flight", "DarkWebMention",
    "TelegramChannel", "TelegramPost",
    "Case", "Note", "Task",
]

_PROPS = [
    "name", "title", "handle", "address", "content", "text",
    "description", "summary", "subject", "body",
    "url", "registration", "owner", "query", "filename",
    "username",
]

# Category → labels mapping (for optional client-side filtering)
CATEGORIES = {
    "case":     ["Case", "Note", "Task"],
    "entity":   ["Person", "Company", "Email", "Phone", "Username",
                 "Account", "IP", "Domain", "Breach", "Leak", "Wallet",
                 "Aircraft", "Flight", "DarkWebMention",
                 "TelegramChannel", "TelegramPost"],
    "location": ["Location"],
    "document": ["Document"],
    "article":  ["Article"],
}

# Human-readable label → category
_LABEL_CATEGORY: dict[str, str] = {}
for _cat, _lbls in CATEGORIES.items():
    for _lbl in _lbls:
        _LABEL_CATEGORY[_lbl] = _cat


# ── Startup helper ────────────────────────────────────────────────────────────

async def ensure_fulltext_index(graph_db) -> None:
    """Create the fulltext index if it doesn't already exist.

    Called once from the FastAPI lifespan so the backend works even on a fresh
    database that hasn't run neo4j/init.cypher yet.
    """
    labels_str = "|".join(_LABELS)
    props_str  = ", ".join(f"n.{p}" for p in _PROPS)

    async with graph_db.driver.session() as session:
        # Check whether it already exists first to avoid a noisy error
        result = await session.run(
            "SHOW INDEXES WHERE name = $name AND type = 'FULLTEXT'",
            name=INDEX_NAME,
        )
        rows = await result.data()
        if rows:
            log.debug("Fulltext index '%s' already exists", INDEX_NAME)
            return

        await session.run(
            f"""
            CREATE FULLTEXT INDEX {INDEX_NAME} IF NOT EXISTS
            FOR (n:{labels_str})
            ON EACH [{props_str}]
            """,
        )
        log.info("Created fulltext index '%s'", INDEX_NAME)


# ── Query builder ─────────────────────────────────────────────────────────────

# Characters that are special in Lucene's classic query parser
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\\/])')


def _escape(token: str) -> str:
    """Escape Lucene special characters in a single token."""
    return _LUCENE_SPECIAL.sub(r"\\\1", token)


def _make_query(raw: str, fuzzy: bool = False) -> str:
    """Convert a plain-text search string into a Lucene query.

    Strategy:
      - Single token  → prefix wildcard: ``token*``
      - Multi-token   → each token ANDed with prefix wildcard: ``tok1* AND tok2*``
      - Quoted phrase → forwarded as-is (user intent is exact phrase)
      - fuzzy=True    → appends ``~0.75`` edit-distance operator per token
                        so "Jonson" matches "Johnson", "Smth" matches "Smith"
    """
    raw = raw.strip()
    if not raw:
        return "*"

    # If the user explicitly quoted something, pass through (strip outer quotes)
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
        inner = raw[1:-1]
        return f'"{_escape(inner)}"'

    tokens = raw.split()
    if fuzzy:
        # Combine prefix wildcard AND fuzzy variant so results include both
        parts = [f"({_escape(t)}* OR {_escape(t)}~0.75)" for t in tokens]
    else:
        parts = [f"{_escape(t)}*" for t in tokens]
    return " AND ".join(parts)


# ── Search ────────────────────────────────────────────────────────────────────

def _first_label(labels: list[str]) -> str:
    """Return the most specific label for a multi-labelled node."""
    # Prefer the first label that isn't a generic helper
    for lbl in labels:
        if lbl not in ("Resource",):
            return lbl
    return labels[0] if labels else "Unknown"


def _node_title(node: dict, label: str) -> str:
    """Best human-readable title for a node, given its primary label."""
    for prop in ("title", "name", "handle", "address", "subject",
                 "registration", "url", "filename", "query"):
        if node.get(prop):
            return str(node[prop])
    return node.get("id", "—")


def _node_snippet(node: dict) -> str | None:
    """Short snippet of body text (truncated to 200 chars)."""
    for prop in ("content", "text", "description", "summary", "body"):
        val = node.get(prop)
        if val and isinstance(val, str) and val.strip():
            s = val.strip().replace("\n", " ")
            return s[:200] + ("…" if len(s) > 200 else "")
    return None


async def full_text_search(
    graph_db,
    query: str,
    limit: int = 40,
    category: str | None = None,
    fuzzy: bool = False,
) -> list[dict]:
    """Run the fulltext query and return normalised result dicts.

    Each result contains:
        id, label, category, title, snippet, score, extra
    """
    if not query or not query.strip():
        return []

    lucene_q = _make_query(query, fuzzy=fuzzy)
    limit = min(max(limit, 1), 200)

    # Build optional label-filter clause
    if category and category in CATEGORIES:
        allowed = set(CATEGORIES[category])
        label_filter = "WHERE any(lbl IN labels(n) WHERE lbl IN $allowed_labels)"
        params: dict = {
            "q": lucene_q,
            "limit": limit,
            "allowed_labels": list(allowed),
        }
    else:
        label_filter = ""
        params = {"q": lucene_q, "limit": limit}

    cypher = f"""
        CALL db.index.fulltext.queryNodes($q_index, $q)
        YIELD node AS n, score
        {label_filter}
        RETURN
            n {{.*}}          AS props,
            labels(n)         AS labels,
            score             AS score
        ORDER BY score DESC
        LIMIT $limit
    """
    params["q_index"] = INDEX_NAME

    results: list[dict] = []
    async with graph_db.driver.session() as session:
        try:
            cursor = await session.run(cypher, **params)
            rows = await cursor.data()
        except Exception as exc:
            log.warning("Fulltext search failed: %s", exc)
            return []

    for row in rows:
        node   = row["props"] or {}
        labels = row["labels"] or []
        score  = row["score"]

        label    = _first_label(labels)
        category_ = _LABEL_CATEGORY.get(label, "entity")
        title    = _node_title(node, label)
        snippet  = _node_snippet(node)

        # Extra context fields (label-specific)
        extra: dict = {}
        if label == "Case":
            extra["status"]   = node.get("status")
            extra["priority"] = node.get("priority")
        elif label == "Note":
            extra["case_id"]   = node.get("case_id")
            extra["note_type"] = node.get("note_type")
        elif label == "Document":
            extra["mime_type"]   = node.get("mime_type")
            extra["vt_status"]   = node.get("vt_status")
            extra["sha256"]      = node.get("sha256")
        elif label in ("Person", "Company"):
            extra["source"] = node.get("source")

        results.append({
            "id":       node.get("id"),
            "label":    label,
            "category": category_,
            "title":    title,
            "snippet":  snippet,
            "score":    round(score, 4),
            "extra":    extra,
        })

    return results


# ── Relationship traversal search ─────────────────────────────────────────────

async def related_entities(
    graph_db,
    entity_id: str,
    depth: int = 2,
    limit: int = 50,
) -> list[dict]:
    """
    Return all nodes reachable from `entity_id` within `depth` hops,
    sorted by number of connections (most connected first).

    Useful for "show me everything connected to X" investigations.
    """
    depth = min(max(depth, 1), 4)
    limit = min(max(limit, 1), 200)

    cypher = """
        MATCH path = (start {id: $id})-[*1..$depth]-(n)
        WHERE n.id IS NOT NULL AND n.id <> $id
        WITH n,
             labels(n)   AS lbls,
             count(path) AS path_count
        RETURN n {.*}       AS props,
               lbls         AS labels,
               path_count   AS connections
        ORDER BY connections DESC
        LIMIT $limit
    """

    results: list[dict] = []
    async with graph_db.driver.session() as session:
        try:
            cursor = await session.run(cypher, id=entity_id, depth=depth, limit=limit)
            rows   = await cursor.data()
        except Exception as exc:
            log.warning("Related-entities search failed: %s", exc)
            return []

    for row in rows:
        node   = row["props"] or {}
        lbls   = row["labels"] or []
        label  = _first_label(lbls)
        cat    = _LABEL_CATEGORY.get(label, "entity")
        title  = _node_title(node, label)
        snippet = _node_snippet(node)

        results.append({
            "id":          node.get("id"),
            "label":       label,
            "category":    cat,
            "title":       title,
            "snippet":     snippet,
            "connections": int(row["connections"]),
            "score":       float(row["connections"]),
            "extra":       {},
        })

    return results

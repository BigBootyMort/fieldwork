"""
Semantic / vector search using Ollama embeddings + Neo4j vector index.

Flow
----
1. At startup, ensure a vector index exists on :VectorIndexed nodes.
2. Pull the embedding model (nomic-embed-text) if not already present.
3. POST /search/index  — triggers a background batch-index of all entities.
4. Entity create/update hooks call index_entity() to keep the index fresh.
5. GET  /search/semantic?q=... — embeds the query and queries the vector index.

Model choice: nomic-embed-text (274 MB, 768 dims, designed for RAG/search).
Fallback: if the model is unavailable, semantic search silently returns [].
"""
import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("fieldwork.semantic")

OLLAMA_URL   = os.getenv("OLLAMA_URL",          "http://ollama:11434")
EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL",  "nomic-embed-text")
VECTOR_DIM   = 768          # nomic-embed-text output dimensionality
VECTOR_INDEX = "entity_vectors"

# Labels whose text will be embedded and indexed
_INDEXED_LABELS = [
    "Person", "Company", "Email", "Phone", "Username", "IP", "Domain",
    "URL", "Wallet", "Location", "Article", "Document", "Note", "Case",
    "DarkWebMention", "TelegramChannel", "Breach",
]

# Properties used to build the text representation of a node
_TEXT_PROPS = [
    "name", "title", "handle", "address", "email", "username",
    "description", "summary", "content", "text", "body",
    "subject", "org", "isp", "country", "city",
]


# ── Startup helpers ───────────────────────────────────────────────────────────

async def ensure_vector_index(graph_db) -> None:
    """Create the single shared vector index if it doesn't exist."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "SHOW INDEXES WHERE name = $name AND type = 'VECTOR'",
            name=VECTOR_INDEX,
        )
        rows = await result.data()
        if rows:
            log.debug("Vector index '%s' already exists", VECTOR_INDEX)
            return
        try:
            await session.run(
                f"""
                CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
                FOR (n:VectorIndexed) ON (n.embedding)
                OPTIONS {{indexConfig: {{
                    `vector.dimensions`: $dim,
                    `vector.similarity_function`: 'cosine'
                }}}}
                """,
                dim=VECTOR_DIM,
            )
            log.info("Created vector index '%s' (%d dims)", VECTOR_INDEX, VECTOR_DIM)
        except Exception as exc:
            log.warning("Vector index creation failed (Neo4j may not support it): %s", exc)


async def pull_embed_model() -> bool:
    """Pull nomic-embed-text from Ollama if not already present. Returns True on success."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            if any(EMBED_MODEL in m for m in models):
                log.info("Embedding model '%s' already present", EMBED_MODEL)
                return True
    except Exception:
        pass

    log.info("Pulling embedding model '%s' — this may take a few minutes…", EMBED_MODEL)
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/pull",
                json={"name": EMBED_MODEL},
            ) as resp:
                async for _ in resp.aiter_lines():
                    pass   # consume the stream
        log.info("Embedding model '%s' ready", EMBED_MODEL)
        return True
    except Exception as exc:
        log.warning("Could not pull '%s': %s — semantic search will be unavailable", EMBED_MODEL, exc)
        return False


# ── Embedding generation ──────────────────────────────────────────────────────

async def generate_embedding(text: str) -> Optional[list[float]]:
    """Return a 768-dim embedding vector or None on failure."""
    if not text or not text.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": text[:2000]},
            )
            r.raise_for_status()
            embeddings = r.json().get("embeddings", [])
            if embeddings and isinstance(embeddings[0], list):
                return embeddings[0]
    except Exception as exc:
        log.debug("Embedding failed: %s", exc)
    return None


def _node_text(props: dict, label: str) -> str:
    """Build a short text representation of a node for embedding."""
    parts = [label]
    for p in _TEXT_PROPS:
        v = props.get(p)
        if v and isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " | ".join(parts)[:1500]


# ── Per-entity indexing ───────────────────────────────────────────────────────

async def index_entity(graph_db, entity_id: str, props: dict, label: str = "") -> bool:
    """Generate and store an embedding for a single entity node."""
    text = _node_text(props, label)
    embedding = await generate_embedding(text)
    if embedding is None:
        return False
    try:
        async with graph_db.driver.session() as session:
            await session.run(
                """
                MATCH (n {id: $id})
                SET n:VectorIndexed, n.embedding = $emb, n.embedding_text = $text
                """,
                id=entity_id, emb=embedding, text=text,
            )
        return True
    except Exception as exc:
        log.warning("Failed to store embedding for %s: %s", entity_id, exc)
        return False


# ── Batch indexing ────────────────────────────────────────────────────────────

async def batch_index_all(graph_db, batch_size: int = 20) -> dict:
    """
    Index all unindexed entity nodes. Runs as a background task.
    Returns {"indexed": N, "failed": N, "skipped": N}.
    """
    label_filter = "|".join(_INDEXED_LABELS)
    stats = {"indexed": 0, "failed": 0, "skipped": 0}
    offset = 0

    while True:
        async with graph_db.driver.session() as session:
            result = await session.run(
                f"""
                MATCH (n)
                WHERE any(l IN labels(n) WHERE l IN $labels)
                  AND NOT n:VectorIndexed
                  AND n.id IS NOT NULL
                RETURN n {{.*}} AS props, labels(n) AS lbls
                SKIP $offset LIMIT $batch
                """,
                labels=_INDEXED_LABELS,
                offset=offset,
                batch=batch_size,
            )
            rows = await result.data()

        if not rows:
            break

        tasks = []
        for row in rows:
            props = dict(row["props"])
            label = next((l for l in (row["lbls"] or []) if l in _INDEXED_LABELS), "Entity")
            tasks.append(index_entity(graph_db, props.get("id", ""), props, label))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if r is True:
                stats["indexed"] += 1
            elif r is False:
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

        offset += batch_size
        await asyncio.sleep(0.1)   # be gentle on Ollama

    log.info("Batch index complete: %s", stats)
    return stats


# ── Semantic search ───────────────────────────────────────────────────────────

async def semantic_search(
    graph_db,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """
    Find entities semantically similar to `query`.
    Returns a list of result dicts with id, label, title, snippet, score.
    """
    embedding = await generate_embedding(query)
    if embedding is None:
        return []

    limit = min(max(limit, 1), 100)

    try:
        async with graph_db.driver.session() as session:
            result = await session.run(
                f"""
                CALL db.index.vector.queryNodes($idx, $limit, $emb)
                YIELD node AS n, score
                RETURN n {{.*}} AS props, labels(n) AS lbls, score
                ORDER BY score DESC
                """,
                idx=VECTOR_INDEX, limit=limit, emb=embedding,
            )
            rows = await result.data()
    except Exception as exc:
        log.warning("Vector search failed: %s", exc)
        return []

    out = []
    for row in rows:
        props = dict(row["props"])
        props.pop("embedding", None)        # don't ship vectors to the client
        lbls  = row["lbls"] or []
        label = next((l for l in lbls if l not in ("VectorIndexed",)), lbls[0] if lbls else "Entity")
        title = (props.get("name") or props.get("title") or props.get("handle")
                 or props.get("address") or props.get("id", "—"))
        snippet = None
        for p in ("description", "summary", "content", "text", "body"):
            v = props.get(p)
            if v and isinstance(v, str) and v.strip():
                snippet = v.strip()[:200] + ("…" if len(v) > 200 else "")
                break

        out.append({
            "id":      props.get("id"),
            "label":   label,
            "title":   title,
            "snippet": snippet,
            "score":   round(float(row["score"]), 4),
            "props":   props,
        })

    return out

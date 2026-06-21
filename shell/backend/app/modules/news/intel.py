"""
News intelligence layer:

  * entity extraction  — lightweight capitalized-phrase NER (no LLM cost)
  * watchlist store     — JSON-backed entities/topics/countries to track
  * story clustering    — group near-duplicate articles via nomic-embed vectors

Kept dependency-free (stdlib + the Ollama embeddings endpoint) so it runs on
every poll without adding latency-heavy LLM calls per article.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path

import httpx

# ── Entity extraction (heuristic) ───────────────────────────────────────────

# Words that start a sentence / are capitalised but aren't entities.
_STOP = {
    "The", "A", "An", "This", "That", "These", "Those", "His", "Her", "Its",
    "Their", "Our", "Your", "My", "But", "And", "Or", "For", "Nor", "So", "Yet",
    "In", "On", "At", "By", "To", "From", "With", "As", "If", "When", "While",
    "After", "Before", "Over", "Under", "New", "Live", "Breaking", "Update",
    "Report", "Reports", "Says", "Said", "How", "Why", "What", "Who", "Where",
    "Mr", "Ms", "Mrs", "Dr", "US", "UK", "EU", "UN",
}
# Sequence of Capitalized words (allowing &, ., -, and lowercase connectors)
_ENT_RE = re.compile(
    r"\b([A-Z][a-zA-Z.&'-]+(?:\s+(?:of|the|and|de|van|von|al|bin)\s+)?"
    r"(?:\s+[A-Z][a-zA-Z.&'-]+){0,4})\b"
)


def extract_entities(title: str, summary: str = "", limit: int = 8) -> list[str]:
    """Return distinct multi-word proper-noun phrases from a headline/summary."""
    text = f"{title}. {summary or ''}"
    seen: dict[str, int] = {}
    # Match within sentence fragments so phrases don't span sentence breaks
    # (avoids noise like "Israel. At Iran" being captured as one entity).
    for sentence in re.split(r"[.!?]\s+|[:;|]\s+|\s[-–—]\s", text):
        for m in _ENT_RE.finditer(sentence):
            phrase = m.group(1).strip(" .,-")
            words = phrase.split()
            # Keep only multi-word phrases, or single words that aren't stopwords
            if len(words) == 1 and (phrase in _STOP or len(phrase) < 4):
                continue
            if all(w in _STOP for w in words):
                continue
            # Strip a leading stopword (e.g. "The Kremlin" -> "Kremlin")
            while words and words[0] in _STOP:
                words = words[1:]
            if not words:
                continue
            phrase = " ".join(words)
            if len(phrase) < 4:
                continue
            seen[phrase] = seen.get(phrase, 0) + 1
    # Rank by frequency then length (longer = more specific)
    ranked = sorted(seen.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return [p for p, _ in ranked[:limit]]


# ── Watchlist store (JSON-file backed, persists across restarts) ────────────

_WL_PATH = Path(__file__).parent / "watchlist.json"


def _wl_load() -> dict:
    try:
        if _WL_PATH.exists():
            return json.loads(_WL_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"items": [], "last_brief_at": 0, "hits": []}


def _wl_save(data: dict) -> None:
    try:
        _WL_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def watchlist_all() -> list[dict]:
    return _wl_load().get("items", [])


def watchlist_add(kind: str, value: str) -> dict:
    kind = kind if kind in ("entity", "topic", "country") else "entity"
    value = value.strip()
    data = _wl_load()
    # de-dup (case-insensitive) within the same kind
    for it in data["items"]:
        if it["kind"] == kind and it["value"].lower() == value.lower():
            return it
    item = {"id": uuid.uuid4().hex[:12], "kind": kind, "value": value,
            "created_at": int(time.time())}
    data["items"].append(item)
    _wl_save(data)
    return item


def watchlist_remove(item_id: str) -> bool:
    data = _wl_load()
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i["id"] != item_id]
    _wl_save(data)
    return len(data["items"]) < before


def match_watchlist(article: dict, entities: list[str] | None = None) -> list[dict]:
    """Return the watchlist items an article matches (entity/topic/country)."""
    items = watchlist_all()
    if not items:
        return []
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or "").lower()
    topic = (article.get("topic") or "").lower()
    countries = {c.upper() for c in (article.get("countries") or [])}
    ents = {e.lower() for e in (entities or article.get("entities") or [])}
    hits = []
    for it in items:
        v = it["value"].lower()
        if it["kind"] == "country" and it["value"].upper() in countries:
            hits.append(it)
        elif it["kind"] == "topic" and (v == topic or v in title or v in summary):
            hits.append(it)
        elif it["kind"] == "entity" and (v in ents or v in title or v in summary):
            hits.append(it)
    return hits


def get_last_brief_at() -> int:
    return _wl_load().get("last_brief_at", 0)


def set_last_brief_at(ts: int | None = None) -> None:
    data = _wl_load()
    data["last_brief_at"] = int(ts if ts is not None else time.time())
    _wl_save(data)


# ── Story clustering via embeddings (nomic-embed) ───────────────────────────

async def embed_text(http: httpx.AsyncClient, ollama_url: str, model: str, text: str) -> list[float]:
    """Return an embedding vector for text, or [] on failure."""
    try:
        r = await http.post(f"{ollama_url.rstrip('/')}/api/embeddings",
                            json={"model": model, "prompt": text[:512]}, timeout=20)
        if r.status_code == 200:
            return r.json().get("embedding", []) or []
    except Exception:
        pass
    return []


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cluster_articles(articles: list[dict], threshold: float = 0.82) -> list[dict]:
    """
    Greedy single-pass clustering of articles that carry an 'embedding'.
    Articles must be pre-sorted by importance (best representative first).
    Returns a list of story clusters:
       {"lead": <article>, "members": [...], "sources": [...], "size": n}
    Articles without embeddings each form their own singleton cluster.
    """
    clusters: list[dict] = []
    for a in articles:
        emb = a.get("embedding") or []
        placed = False
        if emb:
            for c in clusters:
                if c["_emb"] and _cosine(emb, c["_emb"]) >= threshold:
                    c["members"].append(a)
                    src = (a.get("source") or {}).get("name")
                    if src and src not in c["sources"]:
                        c["sources"].append(src)
                    placed = True
                    break
        if not placed:
            src = (a.get("source") or {}).get("name")
            clusters.append({
                "lead": a, "members": [a],
                "sources": [src] if src else [],
                "_emb": emb,
            })
    # Finalise — drop the internal embedding, add size, sort by corroboration
    out = []
    for c in clusters:
        out.append({
            "lead":    c["lead"],
            "members": c["members"],
            "sources": c["sources"],
            "size":    len(c["members"]),
        })
    out.sort(key=lambda c: (c["size"], c["lead"].get("importance", 0)), reverse=True)
    return out

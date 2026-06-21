"""
News module service — RSS ingest, heat aggregation, LLM brief/ask.

Storage model (Neo4j):
  (a:NewsArticle {id, url, title, summary, source_id, published_at, ingested_at})
  (s:NewsSource  {id, name, url, weight, topic})
  (c:NewsCountry {iso, name})
  (a)-[:FROM]->(s)
  (a)-[:MENTIONS_COUNTRY]->(c)

Heat scoring:
  heat(country, window) =
      Σ articles in country [
          source_weight × topic_weight × recency_decay
      ]
  where recency_decay = exp(-age_hours / 12)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from datetime         import datetime, timezone, timedelta
from email.utils      import parsedate_to_datetime
from typing           import Any
from xml.etree        import ElementTree as ET

import httpx

from .geo      import detect_countries, detect_all, country_name
from .sources  import DEFAULT_SOURCES, NewsSource, TOPIC_WEIGHTS, TOPIC_KEYWORDS
from .prompts  import (
    MORNING_BRIEF_SYSTEM, ASSISTANT_SYSTEM, build_articles_context, _utc_now,
)
from .llm      import (
    call_claude, claude_configured,
    call_bridge, bridge_healthy, bridge_enabled,
)
from .intel    import (
    extract_entities, match_watchlist, cluster_articles, embed_text,
    get_last_brief_at, set_last_brief_at,
)

log = logging.getLogger("news.service")


# ── RSS / Atom parsing (stdlib only — no feedparser dep) ────────────────────

# Atom namespace
_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _clean_html(text: str) -> str:
    """Strip HTML tags + collapse whitespace from RSS descriptions."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(s: str) -> datetime:
    """
    Tolerant RFC-822 / ISO-8601 parser. Returns a timezone-aware UTC datetime
    so Neo4j stores it as zoned DateTime (matching query-side `datetime()`).
    Naive datetimes would become LocalDateTime and silently fail comparisons.
    """
    if not s:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_feed(xml_bytes: bytes) -> list[dict]:
    """Return a list of {title, url, summary, published_at} dicts from RSS or Atom."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    out: list[dict] = []

    # RSS 2.0 — channel/item
    for item in root.findall(".//item"):
        title  = (item.findtext("title") or "").strip()
        url    = (item.findtext("link") or "").strip()
        desc   = _clean_html(item.findtext("description") or "")
        pubd   = _parse_date(item.findtext("pubDate") or "")
        if title and url:
            out.append({
                "title": title, "url": url, "summary": desc,
                "published_at": pubd,
            })

    # Atom — entry
    for entry in root.findall("atom:entry", _NS) + root.findall(".//entry"):
        title = (entry.findtext("atom:title", default="", namespaces=_NS) or
                 entry.findtext("title", default="")).strip()
        link_el = entry.find("atom:link", _NS) or entry.find("link")
        url = (link_el.get("href") if link_el is not None and link_el.get("href") else
               (entry.findtext("atom:id", default="", namespaces=_NS) or "")).strip()
        summary = _clean_html(
            entry.findtext("atom:summary", default="", namespaces=_NS) or
            entry.findtext("atom:content", default="", namespaces=_NS) or
            entry.findtext("summary", default="") or
            entry.findtext("content", default="")
        )
        pubd = _parse_date(
            entry.findtext("atom:updated", default="", namespaces=_NS) or
            entry.findtext("atom:published", default="", namespaces=_NS) or
            entry.findtext("updated", default="")
        )
        if title and url:
            out.append({
                "title": title, "url": url, "summary": summary,
                "published_at": pubd,
            })

    return out


# ── Topic classification (keyword-based v1) ─────────────────────────────────

def _classify_topic(source_topic: str, title: str, summary: str) -> tuple[str, float]:
    """
    Return (topic, weight_multiplier).
    Source topic is the default; keyword hits in title/summary can override.
    """
    text = (title + " " + (summary or "")).lower()
    best_topic = source_topic
    best_score = TOPIC_WEIGHTS.get(source_topic, 1.0)
    for topic, kws in TOPIC_KEYWORDS.items():
        if any(kw in text for kw in kws):
            w = TOPIC_WEIGHTS.get(topic, 1.0)
            if w > best_score:
                best_topic, best_score = topic, w
    return best_topic, best_score


def _article_id(url: str) -> str:
    """Deterministic article id from URL (so re-poll dedupes)."""
    return "art:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


# ── Ollama client (minimal — no extra dep) ──────────────────────────────────

class OllamaClient:
    def __init__(self, http: httpx.AsyncClient, url: str, model: str):
        self.http  = http
        self.url   = url.rstrip("/")
        self.model = model

    async def available_models(self) -> list[str]:
        """Return model names currently loaded in Ollama ([] on error)."""
        try:
            r = await self.http.get(f"{self.url}/api/tags", timeout=8)
            if r.status_code == 200:
                return [m.get("name", "") for m in r.json().get("models") or []]
        except Exception:
            pass
        return []

    async def chat(self, system: str, user: str, *, model: str | None = None,
                   temperature: float = 0.3) -> str:
        target = model or self.model
        payload = {
            "model":  target,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "options": {"temperature": temperature},
        }
        r = await self.http.post(f"{self.url}/api/chat", json=payload, timeout=120)

        if r.status_code == 404:
            # Model not pulled — give an actionable error instead of raw HTTP text
            loaded = await self.available_models()
            if loaded:
                hint = (
                    f"Model **{target}** is not loaded in Ollama.  \n"
                    f"Loaded models: {', '.join(loaded)}.  \n"
                    f"To fix, run:  \n"
                    f"`docker compose exec ollama ollama pull {target}`"
                )
            else:
                hint = (
                    f"Model **{target}** not found — no models are loaded in Ollama yet.  \n"
                    f"Run:  \n"
                    f"`docker compose exec ollama ollama pull {target}`  \n"
                    f"(this downloads ~2 GB on first run)"
                )
            raise RuntimeError(hint)

        r.raise_for_status()
        body = r.json()
        return (body.get("message") or {}).get("content", "").strip()


# ── Service ─────────────────────────────────────────────────────────────────

class NewsService:
    """All news operations — RSS ingest, persistence, queries, LLM."""

    def __init__(self, deps: dict[str, Any]):
        self.deps     = deps
        self.driver   = deps["graph_db"]
        self.http     = deps["http"]
        self.settings = deps["settings"]
        self.bus      = deps["bus"]
        self.ollama   = OllamaClient(
            self.http, self.settings.OLLAMA_URL, self.settings.OLLAMA_MODEL,
        )
        # In-memory caches
        self._heatmap_cache: dict[str, tuple[float, list[dict]]] = {}
        self._brief_cache:   tuple[float, tuple[str, str]] | None = None

    # ── Source registry (in-memory; persisted on first use) ────────────────
    def sources(self) -> list[NewsSource]:
        return DEFAULT_SOURCES

    async def upsert_sources(self) -> None:
        async with self.driver.session() as session:
            for s in self.sources():
                await session.run(
                    """
                    MERGE (n:NewsSource {id: $id})
                    SET   n.name=$name, n.url=$url,
                          n.weight=$weight, n.topic=$topic
                    """,
                    id=s.id, name=s.name, url=s.url, weight=s.weight, topic=s.topic,
                )

    # ── Ingest: fetch all RSS feeds, parse, persist ────────────────────────
    async def poll_all(self) -> dict:
        """Poll every default source. Returns per-source counts."""
        await self.upsert_sources()
        per_source: dict[str, int] = {}
        total_new = 0
        for src in self.sources():
            try:
                resp = await self.http.get(src.url, timeout=25,
                                           headers={"User-Agent": "Runi Shell/0.1"})
                resp.raise_for_status()
                items = _parse_feed(resp.content)
            except Exception as exc:
                log.warning("poll %s failed: %s", src.id, exc)
                per_source[src.id] = -1
                continue
            new_count = 0
            for it in items:
                if await self._persist_article(src, it):
                    new_count += 1
            per_source[src.id] = new_count
            total_new += new_count

        # Invalidate caches so the next /heatmap reflects new data
        self._heatmap_cache.clear()
        self._brief_cache = None

        await self.bus.publish("news:polled", {"new": total_new, "per_source": per_source})
        return {"new_total": total_new, "per_source": per_source,
                "polled_at": datetime.utcnow().isoformat() + "Z"}

    async def _persist_article(self, src: NewsSource, it: dict) -> bool:
        """Insert one article. Returns True if it was new.

        Country and source links are always (re-)created with MERGE so they are
        idempotent.  This means re-polling updates links for articles that were
        previously ingested before geo detection was expanded — you don't need
        to drop the graph and re-ingest to get new country associations.
        """
        title   = it["title"][:400]
        url     = it["url"][:600]
        summary = (it.get("summary") or "")[:1200]
        pubd    = it["published_at"]
        aid     = _article_id(url)

        topic, topic_w = _classify_topic(src.topic, title, summary)
        countries      = detect_all(title=title, summary=summary, url=url, limit=6)

        async with self.driver.session() as session:
            r = await session.run(
                """
                MERGE (a:NewsArticle {id: $aid})
                ON CREATE SET
                    a.url=$url, a.title=$title, a.summary=$summary,
                    a.source_id=$sid, a.published_at=$pubd,
                    a.ingested_at=datetime(),
                    a.topic=$topic, a.topic_weight=$tw,
                    a._created = true
                ON MATCH SET
                    a.published_at=$pubd,
                    a.topic=$topic, a.topic_weight=$tw,
                    a._created = false
                WITH a, a._created AS created
                REMOVE a._created
                RETURN created
                """,
                aid=aid, url=url, title=title, summary=summary,
                sid=src.id, pubd=pubd, topic=topic, tw=topic_w,
            )
            rec     = await r.single()
            created = bool(rec and rec["created"])

            # Link to source — always (MERGE is idempotent)
            await session.run(
                """
                MATCH (a:NewsArticle {id:$aid}), (s:NewsSource {id:$sid})
                MERGE (a)-[:FROM]->(s)
                """, aid=aid, sid=src.id,
            )
            # Link to countries — always (MERGE is idempotent).
            # Running this even for existing articles means re-polling picks up
            # any new country associations from improved geo detection.
            for iso in countries:
                await session.run(
                    """
                    MERGE (c:NewsCountry {iso:$iso})
                    ON CREATE SET c.name = $name
                    WITH c
                    MATCH (a:NewsArticle {id:$aid})
                    MERGE (a)-[:MENTIONS_COUNTRY]->(c)
                    """,
                    iso=iso, name=country_name(iso), aid=aid,
                )
        return created

    # ── Queries ────────────────────────────────────────────────────────────
    async def heatmap(self, window_h: int = 12) -> list[dict]:
        """Country heat scores for the choropleth."""
        cache_key = f"heat:{window_h}"
        now = time.time()
        cached = self._heatmap_cache.get(cache_key)
        if cached and (now - cached[0]) < 60:
            return cached[1]

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)

        async with self.driver.session() as session:
            r = await session.run(
                """
                MATCH (a:NewsArticle)-[:MENTIONS_COUNTRY]->(c:NewsCountry)
                MATCH (a)-[:FROM]->(s:NewsSource)
                WHERE a.published_at >= $cutoff
                RETURN c.iso  AS iso,
                       c.name AS name,
                       collect({
                         id:       a.id,
                         title:    a.title,
                         pub_ts:   toInteger(a.published_at.epochSeconds),
                         topic:    coalesce(a.topic, 'world'),
                         topic_w:  coalesce(a.topic_weight, 1.0),
                         src_w:    coalesce(s.weight, 0.7)
                       }) AS arts
                """, cutoff=cutoff,
            )
            records = await r.fetch(500)

        now_ts = datetime.utcnow().timestamp()
        out: list[dict] = []
        for rec in records:
            arts = rec["arts"] or []
            score = 0.0
            top: dict | None = None
            top_recency = -1.0
            # Per-country topic accumulator (weighted by contribution)
            topic_scores: dict[str, float] = {}

            for a in arts:
                age_h = max(0, (now_ts - a["pub_ts"]) / 3600.0)
                decay = math.exp(-age_h / 12.0)
                contribution = float(a["src_w"]) * float(a["topic_w"]) * decay
                score += contribution

                t = a.get("topic", "world")
                topic_scores[t] = topic_scores.get(t, 0.0) + contribution

                # Freshest highest-weighted article = "top headline"
                rank = float(a["src_w"]) * float(a["topic_w"]) - age_h * 0.05
                if rank > top_recency:
                    top_recency = rank
                    top = {"id": a["id"], "title": a["title"], "topic": t}

            dominant_topic = max(topic_scores, key=topic_scores.get) if topic_scores else "world"

            out.append({
                "iso":           rec["iso"],
                "name":          rec["name"],
                "article_count": len(arts),
                "hot_score":     round(score * 10),       # ×10 to land in 0-100 band
                "top_headline":  (top or {}).get("title", ""),
                "top_id":        (top or {}).get("id", ""),
                "top_topic":     (top or {}).get("topic", dominant_topic),
                "dominant_topic": dominant_topic,
            })
        out.sort(key=lambda x: x["hot_score"], reverse=True)

        self._heatmap_cache[cache_key] = (now, out)
        return out

    async def articles(
        self, *, country: str | None = None, topic: str | None = None,
        window_h: int = 24, limit: int = 60,
    ) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
        cypher = """
            MATCH (a:NewsArticle)-[:FROM]->(s:NewsSource)
            WHERE a.published_at >= $cutoff
        """
        params: dict[str, Any] = {"cutoff": cutoff, "limit": limit}
        if country:
            cypher += "\n        MATCH (a)-[:MENTIONS_COUNTRY]->(c:NewsCountry {iso:$iso})"
            params["iso"] = country.upper()
        if topic:
            cypher += "\n        WHERE a.topic = $topic"
            params["topic"] = topic

        cypher += """
            OPTIONAL MATCH (a)-[:MENTIONS_COUNTRY]->(cn:NewsCountry)
            WITH a, s, collect(DISTINCT cn.iso)[..6] AS countries,
                 // Importance = source_weight × topic_weight × recency_decay (12h half-life)
                 coalesce(s.weight, 0.7) *
                 coalesce(a.topic_weight, 1.0) *
                 exp(-1.0 * duration.inSeconds(a.published_at, datetime()).seconds / 43200.0)
                 AS importance
            RETURN a.id AS id, a.title AS title, a.url AS url,
                   a.summary AS summary,
                   a.published_at AS published_at,
                   a.topic AS topic, a.topic_weight AS topic_weight,
                   s.id AS source_id, s.name AS source_name, s.weight AS source_weight,
                   countries,
                   round(importance * 100) / 100.0 AS importance
            ORDER BY importance DESC, a.published_at DESC
            LIMIT $limit
        """

        async with self.driver.session() as session:
            r = await session.run(cypher, **params)
            records = await r.fetch(limit)

        return [
            {
                "id":           rec["id"],
                "title":        rec["title"],
                "url":          rec["url"],
                "summary":      rec["summary"] or "",
                "published_at": str(rec["published_at"]) if rec["published_at"] else "",
                "topic":        rec["topic"],
                "importance":   float(rec.get("importance") or 0.0),
                "source": {
                    "id":     rec["source_id"],
                    "name":   rec["source_name"],
                    "weight": rec["source_weight"],
                },
                "countries":    list(rec["countries"] or []),
                "entities":     extract_entities(rec["title"], rec["summary"] or ""),
            }
            for rec in records
        ]

    # ── Watchlist: live matches over a window ──────────────────────────────
    async def watchlist_hits(self, *, window_h: int = 24, since_ts: int = 0,
                             limit: int = 80) -> list[dict]:
        """Articles in the window that match any watchlist item (live)."""
        arts = await self.articles(window_h=window_h, limit=limit)
        hits = []
        for a in arts:
            if since_ts:
                try:
                    from datetime import datetime as _dt
                    pub = _dt.fromisoformat(str(a["published_at"]).replace("Z", "+00:00"))
                    if pub.timestamp() < since_ts:
                        continue
                except Exception:
                    pass
            matched = match_watchlist(a, a.get("entities"))
            if matched:
                hits.append({
                    "article": {k: a[k] for k in ("id", "title", "url", "source",
                                                  "topic", "published_at", "countries")},
                    "matched": [{"kind": m["kind"], "value": m["value"]} for m in matched],
                })
        return hits

    # ── Stories: embedding-based clustering of near-duplicate articles ──────
    async def stories(self, *, window_h: int = 24, limit: int = 60) -> list[dict]:
        """Cluster articles in the window into de-duplicated stories."""
        arts = await self.articles(window_h=window_h, limit=limit)
        if not arts:
            return []

        embed_model = getattr(self.settings, "OLLAMA_EMBED_MODEL", "") or "nomic-embed-text"

        # Fetch any stored embeddings
        ids = [a["id"] for a in arts]
        stored: dict[str, list] = {}
        try:
            async with self.driver.session() as session:
                r = await session.run(
                    "MATCH (a:NewsArticle) WHERE a.id IN $ids AND a.embedding IS NOT NULL "
                    "RETURN a.id AS id, a.embedding AS emb", ids=ids,
                )
                for rec in await r.fetch(len(ids) or 1):
                    stored[rec["id"]] = list(rec["emb"] or [])
        except Exception as exc:
            log.debug("embedding fetch failed: %s", exc)

        # Lazily embed the ones missing (bounded), then persist
        to_embed = [a for a in arts if a["id"] not in stored][:60]
        if to_embed:
            sem = asyncio.Semaphore(6)

            async def _do(a):
                async with sem:
                    vec = await embed_text(self.http, self.ollama.url, embed_model, a["title"])
                    if vec:
                        stored[a["id"]] = vec
                        try:
                            async with self.driver.session() as s:
                                await s.run("MATCH (a:NewsArticle {id:$id}) SET a.embedding=$e",
                                            id=a["id"], e=vec)
                        except Exception:
                            pass

            await asyncio.gather(*[_do(a) for a in to_embed])

        for a in arts:
            a["embedding"] = stored.get(a["id"], [])

        clusters = cluster_articles(arts)
        # Strip embeddings from the payload
        for a in arts:
            a.pop("embedding", None)
        return clusters

    # ── Investigate bridge → Fieldwork AI Orchestrator ─────────────────────
    async def investigate(self, *, target: str, ttype: str = "auto") -> dict:
        """Hand a news entity to Fieldwork's AI Investigation Orchestrator."""
        base = getattr(self.settings, "FIELDWORK_API", "http://backend:8000").rstrip("/")
        try:
            r = await self.http.post(f"{base}/investigate/orchestrate",
                                     json={"target": target, "type": ttype}, timeout=220)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("investigate proxy failed for %r: %s", target, exc)
            return {"target": target, "error": str(exc),
                    "hint": "Is the Fieldwork backend reachable from the shell?"}

    # ── LLM: status check ──────────────────────────────────────────────────
    async def check_llm(self) -> dict:
        """Return Ollama reachability + whether the configured model is loaded."""
        loaded = await self.ollama.available_models()
        model  = self.ollama.model
        # Ollama stores models as "name:tag" — match on base name too
        ok = any(
            m == model or m.startswith(model + ":") or m.startswith(model.split(":")[0] + ":")
            for m in loaded
        )
        claude = claude_configured()
        bridge = bridge_enabled() and await bridge_healthy(self.http)
        # Active engine reflects the same priority order as _chat()
        engine = ("claude" if claude else
                  "claude-code" if bridge else
                  "ollama" if ok else None)
        return {
            # Overall readiness: any engine means we can brief
            "ok":           bool(claude or bridge or ok),
            "engine":       engine,
            "claude":       claude,
            "claude_code":  bridge,
            "model":        model,
            "loaded":       loaded,
            "ollama_ok":    ok,
            "hint":         (
                None if (claude or bridge or ok) else
                "Start the Claude Code bridge (shell/host-bridge/start-claude-bridge.bat), "
                "add an Anthropic API key in Agent settings, "
                f"or run: docker compose exec ollama ollama pull {model}"
            ),
        }

    # ── LLM: engine selection (Claude → Ollama fallback) ───────────────────
    async def _chat(self, *, system: str, user: str, temperature: float = 0.3,
                    max_tokens: int = 1500) -> tuple[str, str]:
        """
        Run a chat completion with graceful degradation:
            1. Claude API   (if ANTHROPIC_API_KEY set)
            2. Claude Code bridge (host shim → your Claude subscription, no key)
            3. Ollama       (local fallback)
        Returns (text, engine) where engine ∈ {"claude","claude-code","ollama"}.
        """
        # 1. Claude API
        if claude_configured():
            try:
                text = await call_claude(
                    system=system, user=user, http=self.http,
                    temperature=temperature, max_tokens=max_tokens,
                )
                if text:
                    return text, "claude"
                log.warning("Claude API returned empty — trying next engine")
            except Exception as exc:
                log.warning("Claude API failed (%s) — trying next engine", exc)

        # 2. Claude Code bridge (subscription, no API key)
        if bridge_enabled() and await bridge_healthy(self.http):
            try:
                text = await call_bridge(
                    system=system, user=user, http=self.http, max_tokens=max_tokens,
                )
                if text:
                    return text, "claude-code"
            except Exception as exc:
                log.warning("Claude Code bridge failed (%s) — falling back to Ollama", exc)

        # 3. Ollama
        text = await self.ollama.chat(system=system, user=user, temperature=temperature)
        return text, "ollama"

    # ── LLM: morning brief & assistant ─────────────────────────────────────
    async def morning_brief(self, *, window_h: int = 12, force: bool = False) -> dict:
        # 5-minute cache unless force=True
        now = time.time()
        if not force and self._brief_cache and (now - self._brief_cache[0]) < 300:
            brief, engine = self._brief_cache[1]
            return {"brief": brief, "cached": True, "engine": engine}

        articles = await self.articles(window_h=window_h, limit=25)
        if not articles:
            return {"brief": "_No articles ingested yet. Run **Poll feeds** first._",
                    "cached": False, "article_count": 0, "engine": None}

        # Watchlist hits since the last brief — lead with these
        last = get_last_brief_at()
        hits = await self.watchlist_hits(window_h=window_h, since_ts=last)
        wl_block = ""
        if hits:
            lines = [
                f"- {h['article']['title']} "
                f"(matched: {', '.join(m['value'] for m in h['matched'])})"
                for h in hits[:10]
            ]
            wl_block = ("\nWATCHLIST ALERTS (lead the brief with a '## Watchlist alerts' "
                        "section summarising these, newest first):\n" + "\n".join(lines) + "\n")

        context = build_articles_context(articles, max_chars=5000)
        try:
            text, engine = await self._chat(
                system=MORNING_BRIEF_SYSTEM,
                user=f"Date: {_utc_now()}\n{wl_block}\nHeadlines (last {window_h}h):\n\n{context}",
                temperature=0.2,
                max_tokens=1200,
            )
        except Exception as exc:
            return {"brief": f"_LLM error: {exc}_", "cached": False,
                    "article_count": len(articles), "engine": None}

        set_last_brief_at(now)
        self._brief_cache = (now, (text, engine))
        return {"brief": text, "cached": False,
                "article_count": len(articles), "engine": engine,
                "watchlist_hits": len(hits)}

    async def ask(self, *, message: str, visible_article_ids: list[str] | None = None,
                  country: str | None = None) -> dict:
        # Pull context for visible articles, or a default slice if none provided
        if visible_article_ids:
            async with self.driver.session() as session:
                r = await session.run(
                    """
                    MATCH (a:NewsArticle) WHERE a.id IN $ids
                    OPTIONAL MATCH (a)-[:FROM]->(s:NewsSource)
                    OPTIONAL MATCH (a)-[:MENTIONS_COUNTRY]->(c:NewsCountry)
                    RETURN a.id AS id, a.title AS title, a.summary AS summary,
                           s.name AS source, collect(DISTINCT c.iso) AS countries
                    """, ids=visible_article_ids,
                )
                recs = await r.fetch(60)
                articles = [
                    {"id": x["id"], "title": x["title"], "summary": x["summary"] or "",
                     "source": x["source"] or "?", "countries": list(x["countries"] or [])}
                    for x in recs
                ]
        else:
            articles = await self.articles(country=country, window_h=24, limit=30)

        context = build_articles_context(articles, max_chars=6000)
        user_msg = (
            f"User question: {message}\n\n"
            f"Currently-visible article context "
            f"({'all' if not country else 'filtered: ' + country}):\n\n{context}"
        )
        try:
            reply, engine = await self._chat(
                system=ASSISTANT_SYSTEM,
                user=user_msg,
                temperature=0.4,
                max_tokens=1000,
            )
        except Exception as exc:
            return {"reply": f"_LLM error: {exc}_", "context_chars": len(context),
                    "engine": None}

        return {"reply": reply, "context_chars": len(context),
                "article_count": len(articles), "engine": engine}

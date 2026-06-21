"""
Gig Hunter — polls freelance RSS feeds for OSINT/research gigs,
scores them, stores in JSON, and drafts proposals via Ollama.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger("gigs")

# ── Storage ──────────────────────────────────────────────────────────────────
_CACHE_PATH = "/tmp/gigs_cache.json"
_lock = asyncio.Lock()

_EMPTY_CACHE: Dict[str, Any] = {"gigs": {}, "last_refresh": ""}

_store: Dict[str, Any] = {}


def _load_cache() -> Dict[str, Any]:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"gigs": {}, "last_refresh": ""}


def _save_cache(data: Dict[str, Any]) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("gigs: could not save cache: %s", exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gig_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


# ── Scoring ──────────────────────────────────────────────────────────────────

def _score_gig(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 0
    if any(k in text for k in ["osint", "background check", "due diligence", "skip trace", "people search"]):
        score += 40
    elif any(k in text for k in ["investigation", "verify", "research", "find person", "locate"]):
        score += 20
    amounts = re.findall(r'\$(\d+)', text)
    if amounts:
        max_budget = max(int(a) for a in amounts)
        if max_budget >= 200:
            score += 30
        elif max_budget >= 50:
            score += 15
        elif max_budget < 20:
            score -= 20
    if any(k in text for k in ["$5", "$10", "cheap", "quick", "asap", "hurry"]):
        score -= 15
    return max(0, min(100, score))


# ── RSS Sources ──────────────────────────────────────────────────────────────

_QUERIES = ["OSINT", "background check", "due diligence", "research", "investigation", "skip trace"]

_SOURCES = [
    # Upwork removed public RSS (410) — replaced with Reddit OSINT community
    ("reddit_osint",      "https://www.reddit.com/r/OSINT/search.rss?q={query}&sort=new&restrict_sr=on&limit=15"),
    ("reddit_forhire",    "https://www.reddit.com/r/forhire/search.rss?q={query}&sort=new&restrict_sr=on&limit=15"),
    ("reddit_slavelabour","https://www.reddit.com/r/slavelabour/search.rss?q={query}&sort=new&restrict_sr=on&limit=15"),
]


def _parse_rss(platform: str, xml_text: str) -> List[Dict[str, Any]]:
    """Parse RSS XML — tries feedparser first, falls back to ElementTree."""
    items: List[Dict[str, Any]] = []
    try:
        import feedparser
        feed = feedparser.parse(xml_text)
        for entry in feed.entries:
            url   = entry.get("link", "")
            title = entry.get("title", "")
            desc  = entry.get("summary", entry.get("description", ""))
            # strip HTML tags naively
            desc  = re.sub(r'<[^>]+>', ' ', desc)
            pub   = entry.get("published", entry.get("updated", ""))
            items.append({"url": url, "title": title, "description": desc[:800], "posted_at": pub})
    except Exception:
        # Fallback: ElementTree
        try:
            import xml.etree.ElementTree as ET
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(xml_text)
            for item in root.iter("item"):
                url   = (item.findtext("link") or "").strip()
                title = (item.findtext("title") or "").strip()
                desc  = re.sub(r'<[^>]+>', ' ', item.findtext("description") or "")
                pub   = (item.findtext("pubDate") or "").strip()
                if url:
                    items.append({"url": url, "title": title, "description": desc[:800], "posted_at": pub})
        except Exception as exc:
            log.warning("gigs: XML parse failed for %s: %s", platform, exc)
    return items


async def _fetch_source(
    http: httpx.AsyncClient,
    platform: str,
    url_tpl: str,
    query: str,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch one RSS source+query. Returns (items, error_or_None)."""
    from urllib.parse import quote_plus
    url = url_tpl.replace("{query}", quote_plus(query))
    try:
        r = await http.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 Fieldwork-GigHunter/1.0"},
            timeout=15.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return [], f"{platform}/{query}: HTTP {r.status_code}"
        items = _parse_rss(platform, r.text)
        return items, None
    except Exception as exc:
        return [], f"{platform}/{query}: {exc}"


# ── Background refresh ───────────────────────────────────────────────────────

async def _do_refresh(http: httpx.AsyncClient) -> Dict[str, Any]:
    tasks = [
        _fetch_source(http, platform, url_tpl, query)
        for platform, url_tpl in _SOURCES
        for query in _QUERIES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    added  = 0
    errors: List[str] = []

    async with _lock:
        cache = _store.get("data") or _load_cache()

        for (items, err) in results:
            if err:
                errors.append(err)
            for raw in items:
                url = raw.get("url", "")
                if not url:
                    continue
                gid = _gig_id(url)
                existing = cache["gigs"].get(gid)
                if existing and existing.get("status", "new") != "new":
                    continue  # keep non-new statuses as-is
                if gid in cache["gigs"]:
                    continue  # already have it
                score = _score_gig(raw["title"], raw["description"])
                cache["gigs"][gid] = {
                    "id":            gid,
                    "platform":      "reddit_osint" if "r/OSINT" in url else (
                                     "reddit_forhire" if "forhire" in url else (
                                     "reddit_slavelabour" if "slavelabour" in url else "other")),
                    "title":         raw["title"][:300],
                    "description":   raw["description"][:800],
                    "budget":        _extract_budget(raw["title"] + " " + raw["description"]),
                    "url":           url,
                    "posted_at":     raw.get("posted_at", ""),
                    "fetched_at":    _now_iso(),
                    "score":         score,
                    "status":        "new",
                    "proposal_draft": None,
                }
                added += 1

        cache["last_refresh"] = _now_iso()
        _store["data"] = cache
        _save_cache(cache)

    return {"added": added, "total": len(cache["gigs"]), "errors": errors}


def _extract_budget(text: str) -> str:
    amounts = re.findall(r'\$[\d,]+(?:\s*[-–]\s*\$[\d,]+)?(?:\s*/\s*\w+)?', text)
    return amounts[0] if amounts else ""


def _maybe_schedule_refresh(http: httpx.AsyncClient) -> None:
    """Trigger a background refresh if last_refresh is stale (>2h) or missing."""
    cache = _load_cache()
    _store["data"] = cache
    last = cache.get("last_refresh", "")
    if last:
        try:
            dt = datetime.fromisoformat(last)
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
            if age_s < 7200:
                return
        except Exception:
            pass
    log.info("gigs: scheduling startup refresh")
    asyncio.create_task(_do_refresh(http))


# ── Router ───────────────────────────────────────────────────────────────────

class StatusBody(BaseModel):
    status: str


def build_router(deps: dict) -> APIRouter:
    http:     httpx.AsyncClient = deps["http"]
    settings                    = deps["settings"]
    ollama_url:   str = settings.OLLAMA_URL
    ollama_model: str = settings.OLLAMA_MODEL

    router = APIRouter()

    # Lazy-init: trigger a background refresh on first request if cache is stale.
    # (startup events are not available inside router factories)
    _inited = {"done": False}

    def _ensure_init():
        if not _inited["done"]:
            _inited["done"] = True
            _maybe_schedule_refresh(http)

    # ── GET /list ─────────────────────────────────────────────────────────────
    @router.get("/list")
    async def list_gigs(status: Optional[str] = None, min_score: int = 0):
        _ensure_init()
        async with _lock:
            cache = _store.get("data") or _load_cache()
            gigs = list(cache["gigs"].values())

        if status and status != "all":
            gigs = [g for g in gigs if g.get("status") == status]
        gigs = [g for g in gigs if g.get("score", 0) >= min_score]
        gigs.sort(key=lambda g: g.get("score", 0), reverse=True)

        counts: Dict[str, int] = {}
        all_gigs = list(cache.get("gigs", {}).values())
        for g in all_gigs:
            s = g.get("status", "new")
            counts[s] = counts.get(s, 0) + 1

        return {
            "gigs":         gigs,
            "counts":       counts,
            "last_refresh": (cache.get("last_refresh") or ""),
        }

    # ── POST /refresh ─────────────────────────────────────────────────────────
    @router.post("/refresh")
    async def refresh_gigs():
        result = await _do_refresh(http)
        return result

    # ── GET /stats ────────────────────────────────────────────────────────────
    @router.get("/stats")
    async def gig_stats():
        _ensure_init()
        async with _lock:
            cache = _store.get("data") or _load_cache()
            gigs = list(cache["gigs"].values())

        by_status: Dict[str, int]   = {}
        by_platform: Dict[str, int] = {}
        scores: List[int]           = []

        for g in gigs:
            s = g.get("status", "new")
            by_status[s] = by_status.get(s, 0) + 1
            p = g.get("platform", "other")
            by_platform[p] = by_platform.get(p, 0) + 1
            scores.append(g.get("score", 0))

        avg_score = sum(scores) / len(scores) if scores else 0.0
        top_new = sorted(
            [g for g in gigs if g.get("status") == "new"],
            key=lambda g: g.get("score", 0),
            reverse=True,
        )[:3]

        return {
            "total":       len(gigs),
            "by_status":   by_status,
            "by_platform": by_platform,
            "avg_score":   round(avg_score, 1),
            "top_gigs":    top_new,
        }

    # ── GET /{gig_id} ─────────────────────────────────────────────────────────
    @router.get("/{gig_id}")
    async def get_gig(gig_id: str):
        async with _lock:
            cache = _store.get("data") or _load_cache()
        gig = cache["gigs"].get(gig_id)
        if not gig:
            raise HTTPException(404, "Gig not found")
        return gig

    # ── POST /{gig_id}/draft ──────────────────────────────────────────────────
    @router.post("/{gig_id}/draft")
    async def draft_proposal(gig_id: str):
        async with _lock:
            cache = _store.get("data") or _load_cache()
        gig = cache["gigs"].get(gig_id)
        if not gig:
            raise HTTPException(404, "Gig not found")

        system_prompt = (
            "You are an experienced OSINT researcher and professional freelancer. "
            "Write concise, compelling proposals."
        )
        user_prompt = (
            "Write a 120-word freelance proposal for this job posting. "
            "Mention: 24-48h turnaround, complete confidentiality, professional PDF report delivered. "
            "Be specific to their stated needs. Do NOT mention AI tools. "
            f"Gig title: {gig['title']}\n\nGig description:\n{gig['description'][:600]}"
        )

        try:
            payload = {
                "model":  ollama_model,
                "prompt": user_prompt,
                "system": system_prompt,
                "stream": False,
            }
            r = await http.post(f"{ollama_url}/api/generate", json=payload, timeout=120.0)
            r.raise_for_status()
            proposal = r.json().get("response", "").strip()
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}") from exc

        async with _lock:
            cache = _store.get("data") or _load_cache()
            if gig_id in cache["gigs"]:
                cache["gigs"][gig_id]["proposal_draft"] = proposal
                _store["data"] = cache
                _save_cache(cache)

        return {"proposal": proposal, "gig_id": gig_id}

    # ── POST /{gig_id}/status ─────────────────────────────────────────────────
    @router.post("/{gig_id}/status")
    async def set_status(gig_id: str, body: StatusBody):
        valid = {"new", "applied", "won", "lost", "skipped"}
        if body.status not in valid:
            raise HTTPException(400, f"Invalid status. Must be one of: {valid}")
        async with _lock:
            cache = _store.get("data") or _load_cache()
            if gig_id not in cache["gigs"]:
                raise HTTPException(404, "Gig not found")
            cache["gigs"][gig_id]["status"] = body.status
            _store["data"] = cache
            _save_cache(cache)
        return cache["gigs"][gig_id]

    return router

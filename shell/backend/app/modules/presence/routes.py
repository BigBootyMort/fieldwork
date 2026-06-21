"""
FastAPI routes for the Presence module.

Provides:
  POST /generate/post        — generate a social media post
  POST /generate/listing     — generate a platform service listing
  POST /generate/outreach    — generate cold outreach messages
  GET  /calendar             — list saved content items
  POST /calendar             — save a content item
  POST /calendar/{id}/posted — mark item as posted
  DELETE /calendar/{id}      — delete item
  GET  /platforms            — get platform listing statuses
  POST /platforms/{platform} — update a platform status

State is persisted to /tmp/presence_cache.json so it survives route reloads
but is reset on container restart.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib  import Path
from typing   import Dict, List, Optional

import httpx
from fastapi   import APIRouter, HTTPException
from pydantic  import BaseModel

log = logging.getLogger("presence")

_CACHE_FILE = Path("/tmp/presence_cache.json")
_DEFAULT_CACHE: Dict = {"items": [], "platforms": {}}


# ── Persistence helpers ──────────────────────────────────────────────────────

def _load() -> Dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("presence: cache load failed: %s", exc)
    return {"items": [], "platforms": {}}


def _save(data: Dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("presence: cache save failed: %s", exc)


# ── Prompt builders ──────────────────────────────────────────────────────────

def _post_prompt(type_: str, topic: str, platform: str) -> str:
    if type_ == "tip":
        return (
            f"Write a practical OSINT tip about {topic} for a Twitter post. "
            "Under 270 characters. No hashtags. Start with an emoji. "
            "Make it genuinely useful and specific, not generic. Return only the tweet text."
        )
    if type_ == "casestudy":
        return (
            f"Write an anonymised OSINT case study for LinkedIn about {topic}. "
            "120-150 words. Professional tone. Start with a hook. No client names. "
            "End with a call to action. Structure: situation → approach → result."
        )
    if type_ == "thread":
        return (
            f"Write the opening tweet of a Twitter thread about {topic} for an OSINT researcher. "
            "Under 240 characters. Should make people want to read the thread. "
            "Use a number: 'X things about...' or 'A thread on...'. No hashtags."
        )
    # service post
    return (
        "Write a social media post advertising professional OSINT/background check services. "
        "80-150 words. Mention: fast turnaround (24-48h), PDF report, confidential, public sources only. "
        f"Focus on the VALUE to the client. For a {platform} audience. No buzzwords. End with a contact CTA."
    )


def _listing_prompt(platform: str, service: str) -> str:
    return (
        "You are an expert freelance copywriter specialising in research/intelligence services. "
        f"Generate a complete service listing for {platform} for: {service}. "
        "Return JSON with keys: "
        "title (string, under 80 chars), "
        "description (string, 250-400 words), "
        "tags (array of 10 strings), "
        "pricing (object with starter/standard/premium prices and what's included), "
        "faq (array of 3 objects with question and answer). "
        "The service is professional OSINT research, background checks, and due diligence for businesses. "
        "Emphasise: speed, confidentiality, PDF deliverable, public data sources."
    )


def _outreach_prompt(target: str, service: str) -> str:
    return (
        f"Generate cold outreach for an OSINT researcher targeting a {target}. "
        f"Service: {service}. "
        "Return JSON with: "
        "email_subject (string), "
        f"email_body (string, under 200 words, professional, specific pain points for {target}), "
        f"linkedin_message (string, under 290 chars, casual but professional, "
        f"mention a specific relevant use case for {target})."
    )


# ── Ollama call ──────────────────────────────────────────────────────────────

async def _llm(http: httpx.AsyncClient, ollama_url: str, model: str, prompt: str) -> str:
    resp = await http.post(
        f"{ollama_url}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _parse_json(raw: str) -> dict:
    """Extract the first JSON object from an LLM response."""
    s, e = raw.find("{"), raw.rfind("}") + 1
    if s >= 0 and e > s:
        return json.loads(raw[s:e])
    raise ValueError("No JSON object found in LLM response")


# ── Request / response models ────────────────────────────────────────────────

class PostRequest(BaseModel):
    type:     str           # tip | casestudy | thread | service
    topic:    str
    platform: Optional[str] = "LinkedIn"

class ListingRequest(BaseModel):
    platform: str
    service:  str

class OutreachRequest(BaseModel):
    target:  str
    service: str

class CalendarItem(BaseModel):
    text:       str
    type:       str
    created_at: Optional[str] = None

class PlatformStatusRequest(BaseModel):
    status: str             # not_created | draft | live


# ── Router factory ───────────────────────────────────────────────────────────

def build_router(deps: dict) -> APIRouter:
    http:        httpx.AsyncClient = deps["http"]
    settings                       = deps["settings"]
    ollama_url:  str = settings.OLLAMA_URL
    model:       str = settings.OLLAMA_MODEL

    router = APIRouter()

    # ── Generate social post ──────────────────────────────────────────────────
    @router.post("/generate/post")
    async def generate_post(req: PostRequest):
        prompt = _post_prompt(req.type, req.topic, req.platform or "LinkedIn")
        try:
            text = await _llm(http, ollama_url, model, prompt)
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}") from exc
        return {"text": text}

    # ── Generate platform listing ─────────────────────────────────────────────
    @router.post("/generate/listing")
    async def generate_listing(req: ListingRequest):
        prompt = _listing_prompt(req.platform, req.service)
        try:
            raw = await _llm(http, ollama_url, model, prompt)
            data = _parse_json(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(502, f"LLM returned invalid JSON: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}") from exc
        return data

    # ── Generate outreach ─────────────────────────────────────────────────────
    @router.post("/generate/outreach")
    async def generate_outreach(req: OutreachRequest):
        prompt = _outreach_prompt(req.target, req.service)
        try:
            raw = await _llm(http, ollama_url, model, prompt)
            data = _parse_json(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(502, f"LLM returned invalid JSON: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}") from exc
        return data

    # ── Calendar — list ───────────────────────────────────────────────────────
    @router.get("/calendar")
    async def list_calendar():
        cache = _load()
        return {"items": cache.get("items", [])}

    # ── Calendar — save ───────────────────────────────────────────────────────
    @router.post("/calendar")
    async def save_calendar(item: CalendarItem):
        cache = _load()
        entry = {
            "id":         str(uuid.uuid4())[:8],
            "text":       item.text,
            "type":       item.type,
            "created_at": item.created_at or datetime.now(timezone.utc).isoformat(),
            "posted":     False,
        }
        cache.setdefault("items", []).insert(0, entry)
        _save(cache)
        return {"id": entry["id"], "ok": True}

    # ── Calendar — mark posted ────────────────────────────────────────────────
    @router.post("/calendar/{item_id}/posted")
    async def mark_posted(item_id: str):
        cache = _load()
        for item in cache.get("items", []):
            if item["id"] == item_id:
                item["posted"] = True
                _save(cache)
                return {"ok": True}
        raise HTTPException(404, "Item not found")

    # ── Calendar — delete ─────────────────────────────────────────────────────
    @router.delete("/calendar/{item_id}")
    async def delete_item(item_id: str):
        cache = _load()
        before = len(cache.get("items", []))
        cache["items"] = [i for i in cache.get("items", []) if i["id"] != item_id]
        if len(cache["items"]) == before:
            raise HTTPException(404, "Item not found")
        _save(cache)
        return {"ok": True}

    # ── Platforms — get ───────────────────────────────────────────────────────
    @router.get("/platforms")
    async def get_platforms():
        cache = _load()
        return {"platforms": cache.get("platforms", {})}

    # ── Platforms — update ────────────────────────────────────────────────────
    @router.post("/platforms/{platform}")
    async def update_platform(platform: str, req: PlatformStatusRequest):
        allowed = {"not_created", "draft", "live"}
        if req.status not in allowed:
            raise HTTPException(400, f"status must be one of {allowed}")
        cache = _load()
        cache.setdefault("platforms", {})[platform] = req.status
        _save(cache)
        return {"ok": True}

    return router

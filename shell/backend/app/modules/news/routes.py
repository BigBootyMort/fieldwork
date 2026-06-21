"""FastAPI routes for the News module."""
from __future__ import annotations

from typing import Optional, List
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from .service import NewsService
from .intel   import watchlist_all, watchlist_add, watchlist_remove


# Service is instantiated once in init() and captured by the route closures
_service: Optional[NewsService] = None


# ── Pydantic models (module-scope so FastAPI/Pydantic can resolve them) ──
class AskRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    visible_article_ids: List[str] = Field(default_factory=list)
    country: Optional[str] = Field(None, max_length=3)


class WatchAddRequest(BaseModel):
    kind:  str = Field("entity", pattern="^(entity|topic|country)$")
    value: str = Field(..., min_length=1, max_length=120)


class InvestigateRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=300)
    type:   str = Field("auto", max_length=20)


def build_router(service: NewsService) -> APIRouter:
    global _service
    _service = service

    router = APIRouter()

    # ── Source / config ───────────────────────────────────────────────────
    @router.get("/sources")
    async def list_sources():
        return {"sources": [
            {"id": s.id, "name": s.name, "url": s.url,
             "weight": s.weight, "topic": s.topic}
            for s in _service.sources()
        ]}

    # ── Ingest ────────────────────────────────────────────────────────────
    @router.post("/poll")
    async def poll(background: BackgroundTasks, sync: bool = Query(False)):
        """
        Poll all configured RSS feeds.
        sync=true blocks until done (returns counts).
        sync=false (default) runs in background and returns immediately.
        """
        if sync:
            return await _service.poll_all()
        background.add_task(_service.poll_all)
        return {"status": "polling started in background"}

    # ── Queries ───────────────────────────────────────────────────────────
    @router.get("/heatmap")
    async def heatmap(window: int = Query(12, ge=1, le=168)):
        data = await _service.heatmap(window_h=window)
        return {
            "points": data,
            "window_hours": window,
            "country_count": len(data),
        }

    @router.get("/articles")
    async def articles(
        country: Optional[str] = Query(None, max_length=3),
        topic:   Optional[str] = Query(None, max_length=40),
        window:  int = Query(24, ge=1, le=168),
        limit:   int = Query(60, ge=1, le=200),
    ):
        rows = await _service.articles(
            country=country, topic=topic, window_h=window, limit=limit,
        )
        return {"articles": rows, "count": len(rows)}

    # ── LLM ───────────────────────────────────────────────────────────────
    @router.get("/llm-status")
    async def llm_status():
        """Check whether Ollama is running and the configured model is loaded."""
        return await _service.check_llm()

    @router.get("/brief")
    async def get_brief(window: int = Query(12, ge=1, le=48),
                        force:  bool = Query(False)):
        return await _service.morning_brief(window_h=window, force=force)

    @router.post("/ask")
    async def ask(req: AskRequest):
        if not req.message.strip():
            raise HTTPException(400, "message required")
        return await _service.ask(
            message=req.message,
            visible_article_ids=req.visible_article_ids,
            country=req.country,
        )

    # ── Watchlist ──────────────────────────────────────────────────────────
    @router.get("/watchlist")
    async def watchlist_list():
        return {"items": watchlist_all()}

    @router.post("/watchlist")
    async def watchlist_create(req: WatchAddRequest):
        return watchlist_add(req.kind, req.value)

    @router.delete("/watchlist/{item_id}")
    async def watchlist_delete(item_id: str):
        return {"removed": watchlist_remove(item_id)}

    @router.get("/watchlist/hits")
    async def watchlist_hits(window: int = Query(24, ge=1, le=168),
                            since: int = Query(0, ge=0)):
        hits = await _service.watchlist_hits(window_h=window, since_ts=since)
        return {"hits": hits, "count": len(hits)}

    # ── Stories (clustered) ────────────────────────────────────────────────
    @router.get("/stories")
    async def stories(window: int = Query(24, ge=1, le=168),
                     limit: int = Query(60, ge=1, le=120)):
        clusters = await _service.stories(window_h=window, limit=limit)
        return {"stories": clusters, "count": len(clusters)}

    # ── Investigate bridge → Fieldwork Orchestrator ────────────────────────
    @router.post("/investigate")
    async def investigate(req: InvestigateRequest):
        return await _service.investigate(target=req.target, ttype=req.type)

    return router

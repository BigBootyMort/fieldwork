"""FastAPI routes for the Reports module."""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from .service import ReportsService

_service: Optional[ReportsService] = None


def build_router(service: ReportsService) -> APIRouter:
    global _service
    _service = service

    router = APIRouter()

    @router.get("/list")
    async def list_reports():
        """Return all saved reports (most recent first)."""
        return {"reports": await _service.list_reports()}

    @router.get("/generate")
    async def generate(
        window: int  = Query(24, ge=1, le=168,
                             description="News look-back window in hours"),
        save:   bool = Query(True,
                             description="Persist the generated report to Neo4j"),
        force:  bool = Query(False,
                             description="Bypass the 5-minute generation cache"),
    ):
        """
        Generate an intelligence brief from recent news + active cases.
        The result is cached for 5 minutes unless force=true.
        """
        if force:
            _service._cache = None
        report = await _service.generate(window_h=window, save=save)
        return report

    @router.get("/{report_id}")
    async def get_report(report_id: str):
        """Retrieve a single saved report by id."""
        r = await _service.get_report(report_id)
        if not r:
            raise HTTPException(404, "Report not found")
        return r

    @router.delete("/{report_id}")
    async def delete_report(report_id: str):
        """Delete a saved report."""
        ok = await _service.delete_report(report_id)
        if not ok:
            raise HTTPException(404, "Report not found")
        return {"deleted": report_id}

    return router

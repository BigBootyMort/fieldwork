"""
Quant module — thin proxy from the shell to the NautilusTrader `quant-engine` container
(:7005), plus a small saved-strategy store. Research only: the engine has no live-trading
path (no exchange adapters installed), and this module never places orders.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("quant")

_STORE_PATH = "/tmp/quant_strategies.json"
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"strategies": {}}


def _save(d: dict) -> None:
    try:
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as exc:
        log.warning("quant: could not save strategies: %s", exc)


class GenReq(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=1000)
    model: Optional[str] = None


class CodeReq(BaseModel):
    code: str = Field(..., min_length=10, max_length=50000)


class BtReq(BaseModel):
    symbol: str = "BTC"
    interval: str = "1h"
    limit: int = Field(500, ge=50, le=1000)
    code: Optional[str] = None
    params: Optional[dict] = None


class SaveReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = ""
    code: str = ""
    symbol: str = "BTC"
    interval: str = "1h"
    params: Optional[dict] = None
    metrics: Optional[dict] = None


def build_router(deps: dict) -> APIRouter:
    http: httpx.AsyncClient = deps["http"]
    settings = deps["settings"]
    engine = settings.QUANT_ENGINE_URL.rstrip("/")

    router = APIRouter()

    async def _proxy(method: str, path: str, **kw) -> Any:
        try:
            r = await http.request(method, f"{engine}{path}", timeout=300.0, **kw)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, f"quant-engine {exc.response.status_code}")
        except Exception as exc:
            raise HTTPException(502, f"quant-engine unreachable: {exc}")

    # ── engine status + builtins ────────────────────────────────────────────
    @router.get("/engine")
    async def engine_status():
        try:
            r = await http.get(f"{engine}/health", timeout=8.0)
            return r.json()
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    @router.get("/builtins")
    async def builtins():
        return {"builtins": [
            {"id": "ema_cross", "label": "EMA Cross",
             "params": {"fast": 10, "slow": 30, "trade_size": "0.10"}},
        ]}

    # ── generate / validate / backtest (proxied) ─────────────────────────────
    @router.post("/generate")
    async def generate(r: GenReq):
        return await _proxy("POST", "/generate", json=r.model_dump())

    @router.post("/validate")
    async def validate(r: CodeReq):
        return await _proxy("POST", "/validate", json=r.model_dump())

    @router.post("/backtest")
    async def backtest(r: BtReq):
        return await _proxy("POST", "/backtest", json=r.model_dump())

    # ── saved-strategy store ─────────────────────────────────────────────────
    @router.get("/strategies")
    async def list_strategies():
        async with _lock:
            d = _load()
        items = sorted(d["strategies"].values(), key=lambda s: s.get("saved_at", ""), reverse=True)
        return {"strategies": items}

    @router.post("/strategies")
    async def save_strategy(r: SaveReq):
        sid = hashlib.sha256((r.name + _now()).encode()).hexdigest()[:12]
        rec = {"id": sid, **r.model_dump(), "saved_at": _now()}
        async with _lock:
            d = _load()
            d["strategies"][sid] = rec
            _save(d)
        return rec

    @router.delete("/strategies/{sid}")
    async def delete_strategy(sid: str):
        async with _lock:
            d = _load()
            existed = d["strategies"].pop(sid, None) is not None
            if existed:
                _save(d)
        return {"ok": existed}

    return router

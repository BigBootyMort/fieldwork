"""
Shared dependencies that all modules can request via the deps dict.

For the v1 shell we provide:
  - settings  : env-backed config
  - graph_db  : neo4j driver (shared with Fieldwork)
  - httpx     : shared HTTP client for outbound calls
  - audit     : append-only audit log function
  - bus       : in-process pub/sub (cross-module events)

Auth in v1 is delegated to the Fieldwork backend (the shell calls into
Fieldwork's /auth endpoints). When/if the shell grows its own user
registry, get_current_user() lives here.
"""
from __future__ import annotations

import os
import logging
from typing import Any, Callable

import httpx
from neo4j import AsyncGraphDatabase

log = logging.getLogger("shell.deps")


# ── Config ──────────────────────────────────────────────────────────────────
class Settings:
    NEO4J_URI:        str = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
    NEO4J_USER:       str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD:   str = os.getenv("NEO4J_PASSWORD", "changeme_dev_only")
    RUNI_API:    str = os.getenv("RUNI_API", "http://backend:8000")
    RUNI_FRONT:  str = os.getenv("RUNI_FRONT", "http://localhost:3000")
    OLLAMA_URL:       str = os.getenv("OLLAMA_URL", "http://ollama:11434")
    OLLAMA_MODEL:     str = os.getenv("OLLAMA_MODEL", "llama3.2")
    # Quant module — NautilusTrader backtest engine sibling container.
    QUANT_ENGINE_URL: str = os.getenv("QUANT_ENGINE_URL", "http://quant-engine:7005")
    # ── Agent integrations ────────────────────────────────────────────────────
    GITHUB_TOKEN:      str = os.getenv("GITHUB_TOKEN", "")
    ALPACA_API_KEY:    str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_API_SECRET: str = os.getenv("ALPACA_API_SECRET", "")
    ALPACA_BASE_URL:   str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    AGENT_WORKSPACE:   str = os.getenv("AGENT_WORKSPACE", "/tmp/agent_ws")


# ── Event bus ───────────────────────────────────────────────────────────────
class EventBus:
    """Minimal in-process pub/sub for cross-module signals."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[dict], Any]]] = {}

    def subscribe(self, event: str, fn: Callable[[dict], Any]) -> None:
        self._subs.setdefault(event, []).append(fn)

    async def publish(self, event: str, payload: dict) -> None:
        for fn in self._subs.get(event, []):
            try:
                res = fn(payload)
                # tolerate sync or async handlers
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:
                log.warning("bus handler for %s failed: %s", event, exc)


# ── Audit log (writes to Neo4j as AuditLog nodes) ───────────────────────────
async def audit(
    driver, *, action: str, subject: str, user_id: str = "shell",
    detail: str = "",
) -> None:
    try:
        async with driver.session() as session:
            await session.run(
                """
                CREATE (a:AuditLog {
                    id:        randomUUID(),
                    action:    $action,
                    subject:   $subject,
                    user_id:   $uid,
                    detail:    $detail,
                    timestamp: datetime()
                })
                """,
                action=action, subject=subject, uid=user_id, detail=detail,
            )
    except Exception as exc:
        log.warning("audit write failed: %s", exc)


# ── Single entry point used by main.py ──────────────────────────────────────
def build_deps() -> dict[str, Any]:
    settings = Settings()
    driver   = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    http     = httpx.AsyncClient(timeout=30)
    bus      = EventBus()

    return {
        "settings": settings,
        "graph_db": driver,
        "http":     http,
        "bus":      bus,
        "audit":    lambda **kw: audit(driver, **kw),
        "log":      log,
    }

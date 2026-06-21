"""
News module LLM helpers — thin adapters over the shared app-level
`llm_bridge` so there's a single source of truth for Claude API + the
Claude Code subscription bridge.

The News service keeps its own Ollama fallback, so these helpers only cover
the two Claude engines. Public names are kept stable for service.py imports.
"""
from __future__ import annotations

import httpx

from llm_bridge import (  # shared app-level module (app/ is on sys.path)
    api_configured as claude_configured,
    call_claude_api,
    call_bridge,
    bridge_healthy,
    bridge_enabled,
    get_cfg,
)

__all__ = [
    "claude_configured", "call_claude", "call_bridge",
    "bridge_healthy", "bridge_enabled", "get_cfg",
]


async def call_claude(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.3, max_tokens: int = 1500, model: str | None = None,
) -> str:
    """Claude API call (name kept for backwards-compat with the service)."""
    return await call_claude_api(
        system=system, user=user, http=http,
        temperature=temperature, max_tokens=max_tokens, model=model,
    )

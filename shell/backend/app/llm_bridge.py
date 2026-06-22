"""
Shared Claude access layer for all shell modules.

Resolves "Claude" to the first available of:
    1. Claude API          — if ANTHROPIC_API_KEY is configured (Agent settings)
    2. Claude Code bridge  — host shim (`claude -p`) using your subscription, no key

Callers that also have a local fallback (Ollama) catch `NoClaudeError` and
degrade. `claude_complete()` returns (text, engine) so the UI can show which
engine answered: "claude" (API) or "claude-code" (subscription bridge).

Config is read from agent_config.json (shared with the Agent settings UI),
then process env, then defaults.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("llm_bridge")

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BRIDGE_URL = "http://host.docker.internal:8088"

# Shared with the Agent settings UI and the Markets module.
_AGENT_CONFIG_FILE = Path(__file__).parent / "modules" / "agent" / "agent_config.json"


class NoClaudeError(RuntimeError):
    """Raised when no Claude engine (API key or bridge) is available."""


# ── Config ──────────────────────────────────────────────────────────────────

def _load_agent_config() -> dict:
    try:
        if _AGENT_CONFIG_FILE.exists():
            return json.loads(_AGENT_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("agent_config load failed: %s", exc)
    return {}


def get_cfg(key: str, default: str = "") -> str:
    """agent_config.json → process env → default."""
    cfg = _load_agent_config()
    return cfg.get(key) or os.environ.get(key) or default


# ── Claude API ──────────────────────────────────────────────────────────────

def api_configured() -> bool:
    return bool(get_cfg("ANTHROPIC_API_KEY"))


def _auth_headers(api_key: str) -> dict:
    """sk-ant-oat (subscription OAuth) → Bearer + oauth beta; else x-api-key."""
    h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if api_key.startswith("sk-ant-oat"):
        h["authorization"] = f"Bearer {api_key}"
        h["anthropic-beta"] = "oauth-2025-04-20"
    else:
        h["x-api-key"] = api_key
    return h


async def call_claude_api(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.3, max_tokens: int = 1500, model: str | None = None,
) -> str:
    api_key = get_cfg("ANTHROPIC_API_KEY")
    if not api_key:
        raise NoClaudeError("ANTHROPIC_API_KEY not configured")
    target = model or get_cfg("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    headers = _auth_headers(api_key)
    payload = {
        "model": target,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    r = await http.post(ANTHROPIC_API, headers=headers, json=payload, timeout=120.0)
    r.raise_for_status()
    data = r.json()
    return (data.get("content") or [{}])[0].get("text", "").strip()


# ── Claude Code bridge (host shim → subscription) ───────────────────────────

def bridge_url() -> str:
    return get_cfg("CLAUDE_BRIDGE_URL", DEFAULT_BRIDGE_URL).rstrip("/")


def bridge_enabled() -> bool:
    """Opt-out: enabled unless explicitly turned off."""
    return get_cfg("CLAUDE_BRIDGE_ENABLED", "auto").lower() not in (
        "0", "false", "off", "no",
    )


async def bridge_healthy(http: httpx.AsyncClient) -> bool:
    """Reachable AND the host CLI is logged in."""
    if not bridge_enabled():
        return False
    try:
        r = await http.get(f"{bridge_url()}/health", timeout=4.0)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


async def call_bridge(
    *, system: str, user: str, http: httpx.AsyncClient,
    max_tokens: int = 1500, model: str | None = None,
) -> str:
    payload: dict = {"system": system, "prompt": user, "max_tokens": max_tokens}
    if model:
        payload["model"] = model
    r = await http.post(f"{bridge_url()}/complete", json=payload, timeout=185.0)
    r.raise_for_status()
    text = (r.json().get("text") or "").strip()
    if not text:
        raise RuntimeError("bridge returned empty text")
    return text


# ── Unified entry point ─────────────────────────────────────────────────────

async def claude_complete(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.3, max_tokens: int = 1500, model: str | None = None,
) -> tuple[str, str]:
    """
    Try Claude API, then the Claude Code bridge.
    Returns (text, engine) with engine ∈ {"claude", "claude-code"}.
    Raises NoClaudeError if neither engine is available (so callers can
    fall back to a local model). Genuine API/bridge errors propagate.
    """
    # 1. Claude API
    if api_configured():
        try:
            text = await call_claude_api(
                system=system, user=user, http=http,
                temperature=temperature, max_tokens=max_tokens, model=model,
            )
            if text:
                return text, "claude"
            log.warning("Claude API returned empty — trying bridge")
        except NoClaudeError:
            pass
        except Exception as exc:
            log.warning("Claude API failed (%s) — trying bridge", exc)

    # 2. Claude Code bridge
    if bridge_enabled() and await bridge_healthy(http):
        text = await call_bridge(
            system=system, user=user, http=http, max_tokens=max_tokens, model=model,
        )
        if text:
            return text, "claude-code"

    raise NoClaudeError("No Claude engine available (no API key, bridge offline/unauthed)")


async def status(http: httpx.AsyncClient) -> dict:
    """Lightweight availability report for UIs."""
    api = api_configured()
    bridge = bridge_enabled() and await bridge_healthy(http)
    return {
        "claude_api":  api,
        "claude_code": bridge,
        "engine":      "claude" if api else ("claude-code" if bridge else None),
        "available":   api or bridge,
    }

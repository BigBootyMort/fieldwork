"""
Claude access layer for the Fieldwork backend (port 8000 container).

Mirrors the shell's llm_bridge but reads config from this container's
environment (ANTHROPIC_API_KEY is injected from runtime_api_keys.json by
main.py at import time). Resolves "Claude" to the first available of:

    1. Claude API          — if ANTHROPIC_API_KEY is set
    2. Claude Code bridge  — host shim (`claude -p`) using your subscription

Callers fall back to local Ollama on NoClaudeError. `claude_complete()`
returns (text, engine) with engine ∈ {"claude","claude-code"}.

The host bridge is reached at http://host.docker.internal:8088 by default
(works on Docker Desktop without extra_hosts).
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("fieldwork.llm_bridge")

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_BRIDGE_URL = "http://host.docker.internal:8088"


class NoClaudeError(RuntimeError):
    """Raised when no Claude engine (API key or bridge) is available."""


def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Claude API ──────────────────────────────────────────────────────────────

def api_configured() -> bool:
    return bool(_cfg("ANTHROPIC_API_KEY").strip())


async def call_claude_api(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.2, max_tokens: int = 2048, model: str | None = None,
) -> str:
    api_key = _cfg("ANTHROPIC_API_KEY").strip()
    if not api_key:
        raise NoClaudeError("ANTHROPIC_API_KEY not configured")
    target = model or _cfg("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
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


async def call_claude_api_vision(
    *, image_b64: str, media_type: str, prompt: str, system: str,
    http: httpx.AsyncClient, max_tokens: int = 1500, model: str | None = None,
) -> str:
    """Multimodal Claude call (image + text). Requires ANTHROPIC_API_KEY."""
    api_key = _cfg("ANTHROPIC_API_KEY").strip()
    if not api_key:
        raise NoClaudeError("ANTHROPIC_API_KEY not configured (vision needs the API)")
    target = model or _cfg("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    payload = {
        "model": target, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }
    r = await http.post(ANTHROPIC_API, headers=headers, json=payload, timeout=120.0)
    r.raise_for_status()
    return (r.json().get("content") or [{}])[0].get("text", "").strip()


# ── Claude Code bridge ──────────────────────────────────────────────────────

def bridge_url() -> str:
    return _cfg("CLAUDE_BRIDGE_URL", DEFAULT_BRIDGE_URL).rstrip("/")


def bridge_enabled() -> bool:
    return _cfg("CLAUDE_BRIDGE_ENABLED", "auto").lower() not in (
        "0", "false", "off", "no",
    )


async def bridge_healthy(http: httpx.AsyncClient) -> bool:
    if not bridge_enabled():
        return False
    try:
        r = await http.get(f"{bridge_url()}/health", timeout=4.0)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


async def call_bridge(
    *, system: str, user: str, http: httpx.AsyncClient,
    max_tokens: int = 2048, model: str | None = None,
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
    temperature: float = 0.2, max_tokens: int = 2048, model: str | None = None,
) -> tuple[str, str]:
    """
    Try Claude API, then the Claude Code bridge. Returns (text, engine).
    Raises NoClaudeError if neither is available (caller uses Ollama).
    """
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

    if bridge_enabled() and await bridge_healthy(http):
        text = await call_bridge(
            system=system, user=user, http=http, max_tokens=max_tokens, model=model,
        )
        if text:
            return text, "claude-code"

    raise NoClaudeError("No Claude engine available (no API key, bridge offline/unauthed)")


async def status(http: httpx.AsyncClient) -> dict:
    api = api_configured()
    bridge = bridge_enabled() and await bridge_healthy(http)
    return {
        "claude_api":  api,
        "claude_code": bridge,
        "engine":      "claude" if api else ("claude-code" if bridge else None),
        "available":   api or bridge,
    }

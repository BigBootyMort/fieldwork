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
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BRIDGE_URL = "http://host.docker.internal:8088"


class NoClaudeError(RuntimeError):
    """Raised when no Claude engine (API key or bridge) is available."""


def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Claude API ──────────────────────────────────────────────────────────────

def api_configured() -> bool:
    return bool(_cfg("ANTHROPIC_API_KEY").strip())


def _auth_headers(api_key: str) -> dict:
    """
    Pick the right auth scheme for the credential:
      * sk-ant-oat...  — a Claude subscription OAuth token (from `claude
        setup-token`). Authenticate like Claude Code: Bearer + oauth beta.
      * otherwise       — a standard API key via x-api-key.
    """
    h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if api_key.startswith("sk-ant-oat"):
        h["authorization"] = f"Bearer {api_key}"
        h["anthropic-beta"] = "oauth-2025-04-20"
    else:
        h["x-api-key"] = api_key
    return h


async def call_claude_api(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.2, max_tokens: int = 2048, model: str | None = None,
) -> str:
    api_key = _cfg("ANTHROPIC_API_KEY").strip()
    if not api_key:
        raise NoClaudeError("ANTHROPIC_API_KEY not configured")
    target = model or _cfg("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
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


async def call_claude_api_vision(
    *, image_b64: str, media_type: str, prompt: str, system: str,
    http: httpx.AsyncClient, max_tokens: int = 1500, model: str | None = None,
) -> str:
    """Multimodal Claude call (image + text). Requires ANTHROPIC_API_KEY."""
    api_key = _cfg("ANTHROPIC_API_KEY").strip()
    if not api_key:
        raise NoClaudeError("ANTHROPIC_API_KEY not configured (vision needs the API)")
    target = model or _cfg("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    headers = _auth_headers(api_key)
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


# ── OmniRoute (OpenAI-compatible multi-provider gateway) ────────────────────
# Optional tier: if OMNIROUTE_BASE_URL is set, completions can route through an
# OmniRoute gateway (smart routing / fallback / caching across many providers) via
# its OpenAI-compatible /chat/completions endpoint. Sits BELOW the user's own Claude
# (API + bridge) and ABOVE the local Ollama fallback — a no-op when unconfigured.
# Point OMNIROUTE_BASE_URL at a hosted OmniRoute or a self-run instance (its origin,
# incl. any /v1). Set OMNIROUTE_MODEL to a model id valid in your OmniRoute.

def omniroute_configured() -> bool:
    return bool(_cfg("OMNIROUTE_BASE_URL").strip())


async def call_omniroute(
    *, system: str, user: str, http: httpx.AsyncClient,
    temperature: float = 0.2, max_tokens: int = 2048,
) -> str:
    base = _cfg("OMNIROUTE_BASE_URL").strip().rstrip("/")
    if not base:
        raise NoClaudeError("OMNIROUTE_BASE_URL not configured")
    target = _cfg("OMNIROUTE_MODEL").strip()
    if not target:
        raise NoClaudeError("OMNIROUTE_MODEL not set (pick a model id valid in your OmniRoute)")
    headers = {"content-type": "application/json"}
    key = _cfg("OMNIROUTE_API_KEY").strip()
    if key:
        headers["authorization"] = f"Bearer {key}"
    payload = {
        "model": target,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    r = await http.post(f"{base}/chat/completions", headers=headers, json=payload, timeout=120.0)
    r.raise_for_status()
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


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

    # Optional OmniRoute tier (multi-provider gateway) before giving up to Ollama.
    # `model` here is a Claude id, so we don't pass it — OmniRoute uses OMNIROUTE_MODEL.
    if omniroute_configured():
        try:
            text = await call_omniroute(
                system=system, user=user, http=http,
                temperature=temperature, max_tokens=max_tokens,
            )
            if text:
                return text, "omniroute"
        except NoClaudeError:
            pass
        except Exception as exc:
            log.warning("OmniRoute failed (%s) — falling through", exc)

    raise NoClaudeError("No Claude/OmniRoute engine available (caller falls back to Ollama)")


async def status(http: httpx.AsyncClient) -> dict:
    api = api_configured()
    bridge = bridge_enabled() and await bridge_healthy(http)
    omni = omniroute_configured()
    return {
        "claude_api":  api,
        "claude_code": bridge,
        "omniroute":   omni,
        "engine":      "claude" if api else ("claude-code" if bridge else ("omniroute" if omni else None)),
        "available":   api or bridge or omni,
    }

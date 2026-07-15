# LLM engine resolution

_Last verified: 2026-07-11_

All AI features resolve an engine through the same chain. First available wins:

1. **Claude API** — if `ANTHROPIC_API_KEY` is set. Default model:
   `claude-haiku-4-5-20251001` (the older `claude-3-5-haiku-20241022` id **404s** for
   subscription OAuth tokens, so do not revert it). Auth is credential-aware
   (`_auth_headers`): a subscription **OAuth token** (`sk-ant-oat…`, from
   `claude setup-token`) is sent as `Authorization: Bearer …` + `anthropic-beta:
   oauth-2025-04-20` (how Claude Code auths); a normal API key (`sk-ant-api…`) uses
   `x-api-key`. Both work against `api.anthropic.com/v1/messages`.
2. **Claude Code bridge** — host-side shim wrapping `claude -p` so the user's Claude
   subscription powers the app without pasting a token. (commit ffbf0f7)
3. **Ollama** — local fallback in Docker (`http://ollama:11434`, default `llama3.2`).
   Always available; callers catch `NoClaudeError` and degrade to it.

## TWO bridge modules (different config stores)

There are two `llm_bridge.py`, one per backend — they read the key from **different**
places, so a key set in one app does not appear in the other:

| File | Used by | Reads `ANTHROPIC_API_KEY` from |
|---|---|---|
| `shell/backend/app/llm_bridge.py` | News, Markets, Agent, Reports, Gigs, Presence | Agent settings UI → `modules/agent/agent_config.json`, then env |
| `backend/app/llm_bridge.py` | Legacy Fieldwork (orchestrator, `llm.py`, vision) | `backend/app/runtime_api_keys.json` (loaded to env at import), then env |

**Single-source via `.env` (recommended):** both backends declare
`ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}` in `docker-compose.yml`, so one
`ANTHROPIC_API_KEY=` line in `.env` powers Claude everywhere (this is what makes the shell
use Claude instead of falling back to Ollama). The per-app stores above still work and take
precedence over env: the Agent Settings UI (`agent_config.json`) for the shell, and
`runtime_api_keys.json` for legacy. Changing `.env` needs a container **recreate**
(`docker compose up -d backend shell-backend`), not just a restart.

Both expose `claude_complete()` → `(text, engine)` with engine `"claude"` (API) or
`"claude-code"` (bridge); raise `NoClaudeError` when neither Claude engine is available.
The **Fieldwork** bridge additionally has `call_claude_api_vision()` (multimodal) — image
analysis is **API-only** (the text `claude -p` bridge can't see images), so Vision Intel
needs a token/key, not just the bridge.

## The bridge: `shell/host-bridge/claude_bridge.py`

- Runs on the **Windows host**, not Docker (needs the host's `claude` CLI login).
  Start via `start-claude-bridge.bat` or `python claude_bridge.py`; listens on 8088.
- Backend reaches it at `http://host.docker.internal:8088` (`CLAUDE_BRIDGE_URL`);
  `CLAUDE_BRIDGE_ENABLED=off` disables that tier.
- One-time auth: `claude setup-token` (long-lived token for headless `claude -p`).
- Caveats: subscription rate limits (Pro resets ~5 h); CLI startup overhead per call;
  single-user low-volume use only.

## Translation

`libretranslate` container (port 5000) handles foreign-language news; separate from the
LLM chain.

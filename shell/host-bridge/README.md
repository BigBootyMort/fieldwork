# Claude Code bridge

Use your **Claude Pro/Max subscription** to power the dashboard's AI features
(News brief, assistant, Markets analysis) **without an API key**.

It's a tiny host-side HTTP shim that wraps the `claude` CLI in headless mode
(`claude -p`). The Dockerized backend calls it at
`http://host.docker.internal:8088`; if it's down, unauthenticated, or
rate-limited, the backend automatically falls back to local Ollama.

## One-time setup

1. **Install Claude Code** (you already have it if `claude --version` works).
2. **Authenticate the CLI for headless use** — this is the key step:
   ```
   claude setup-token
   ```
   This requires a Claude subscription and stores a long-lived token, so
   `claude -p` works non-interactively. (Alternatively `claude auth login`.)
   Verify with:
   ```
   claude auth status      # should show "loggedIn": true
   ```

## Run it

Double-click **`start-claude-bridge.bat`**, or:
```
python claude_bridge.py
```
Keep the window open while you use AI features. You should see:
```
auth: logged in ✓  (using your Claude subscription)
Claude Code bridge listening on http://0.0.0.0:8088
```

Then in the dashboard, hit **🧠 Brief** — the badge reads **✦ Claude Code**.

## How the backend chooses an engine

Priority order (first available wins):
1. **Claude API** — if `ANTHROPIC_API_KEY` is set in Agent settings
2. **Claude Code bridge** — if this shim is running and logged in
3. **Ollama** — local fallback, always available

## Config (env vars, optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `CLAUDE_BRIDGE_PORT` | `8088` | Port to listen on |
| `CLAUDE_BRIDGE_TIMEOUT` | `180` | Per-request timeout (s) |
| On the backend: `CLAUDE_BRIDGE_URL` | `http://host.docker.internal:8088` | Where the backend looks for the bridge |
| On the backend: `CLAUDE_BRIDGE_ENABLED` | `auto` | Set `off` to disable the bridge engine |

## Caveats

- Claude Code's subscription is intended for interactive coding; using it as an
  app backend is a grey area and **rate-limited** (Pro resets ~every 5 h). Fine
  for single-user, low-volume use.
- The bridge must run on the **host**, not in Docker (it needs your CLI login).
- Slightly slower per call than the raw API (CLI start-up overhead).

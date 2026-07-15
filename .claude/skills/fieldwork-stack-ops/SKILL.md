---
name: fieldwork-stack-ops
description: >-
  Run and troubleshoot the Fieldwork docker-compose stack (WSL2 backend, ~13 services, all
  bound to 127.0.0.1). Use this skill whenever the task is to start/stop/rebuild/restart a
  service, decide whether a code change needs a rebuild vs a restart vs just a refresh, read
  container logs, check what's running, exec into Neo4j/Ollama, apply a schema change to the
  live DB, or recover when Docker/WSL won't start. Triggers include "docker compose",
  "rebuild the backend", "restart shell-backend", "my change isn't showing up", "check the
  logs", "is the stack up", "ports", "start the app", "Docker won't start / WSL error", or
  any container/ops question. Pair with the build skills (runi-shell-module,
  fieldwork-osint-tool) once code is written and needs to run.
---

# Fieldwork stack operations

Single-user, localhost-only. `docker-compose.yml` at the repo root; every port binds
`127.0.0.1`. Main entry point is the **shell at http://localhost:3001**.

## Service & port map

| Service | Port(s) | Notes |
|---|---|---|
| `neo4j` / `neo4j-init` | 7474 (browser), 7687 (bolt) | Shared datastore. `neo4j-init` runs `init.cypher` **once** on a fresh volume, then exits. |
| `backend` (legacy) | 8000 | uvicorn **`--reload`**. `docs`: /docs |
| `frontend` (legacy) | 3000 | nginx static, no-cache headers |
| `shell-backend` | 8002 | uvicorn **no reload**. `docs`: /docs, module list: /api/shell/modules |
| `shell-frontend` | 3001 | nginx SPA + catch-all `/api/` proxy → shell-backend |
| `spiderfoot` | 5001 | OSINT scanner |
| `ollama` / `ollama-init` | 11434 | local LLM fallback; `-init` pulls models |
| `libretranslate` | 5000 | news translation |
| `maigret` / `theharvester` / `recon` | — | aux OSINT tools |
| _host process_ | 8088 | Claude Code bridge — **not** in compose (see below) |

## The apply-a-change matrix (the part that trips people up)

Source dirs are volume-mounted, so "no rebuild" ≠ "change is live". What you do depends on
the service:

| Changed | To apply |
|---|---|
| `backend/app/**` (legacy) | **Nothing** — uvicorn `--reload` + mount picks it up on save. |
| `shell/backend/app/**` | **`docker compose restart shell-backend`** — mounted, but uvicorn has **no** `--reload`, so the running process won't see the edit until restart. A new module import in `main.py` needs this too. |
| `frontend/index.html`, `frontend/static/**` | Hard refresh (Ctrl+Shift+R). Mounted; nginx sends no-cache. |
| `shell/frontend/**` (index.html, modules, shell.js, style.css) | Hard refresh. Mounted. |
| `nginx.conf` (either) | `docker compose restart <frontend-svc>` (nginx reads config at start). |
| Python deps / any `Dockerfile` / new pip package | `docker compose up -d --build <svc>` (rebuild the image). |
| `neo4j/init.cypher` | Only re-runs on a **fresh** volume. Apply the statement to the live DB by hand (see Neo4j below) or wipe the volume. |

Rule of thumb: **`--build` is only for dependency/image changes.** For ordinary code edits
it's either nothing (legacy backend / frontends) or a plain `restart` (shell backend).

## Everyday commands

```bash
docker compose ps                              # what's up
docker compose up -d                           # start everything (detached)
docker compose up -d --build shell-backend shell-frontend   # rebuild + restart the shell
docker compose restart shell-backend           # apply a shell backend code edit
docker compose logs -f --tail=100 shell-backend   # follow logs
docker compose down                            # stop all (keeps volumes/data)
```

Neo4j (inspect data / apply a schema change without wiping the volume):

```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "CREATE CONSTRAINT foo_id IF NOT EXISTS FOR (f:Foo) REQUIRE f.id IS UNIQUE;"
```

or open the browser UI at http://localhost:7474. (Graph schema/query details are the
`fieldwork-graph` skill.)

## The Claude Code bridge (host, not Docker)

`shell/host-bridge/claude_bridge.py` runs on the **Windows host** (it needs the host's
`claude` CLI login), listening on **8088**; backends reach it at
`http://host.docker.internal:8088`. Start it with `shell/host-bridge/start-claude-bridge.bat`
(or `python claude_bridge.py`). One-time: `claude setup-token`. `CLAUDE_BRIDGE_ENABLED=off`
disables that LLM tier. It is *not* managed by compose — starting the stack does not start
the bridge. See `docs/kb/llm-engines.md`.

## Data & keys

- Neo4j data persists in a named volume across `down`/`up`. `docker compose down -v`
  **wipes** it and re-runs `init.cypher` on next start — only do that intentionally.
- Optional API keys live in `backend/app/runtime_api_keys.json` (legacy) and the shell's
  `modules/agent/agent_config.json` — **two separate stores** (see `docs/kb/llm-engines.md`).
  The legacy backend now injects its runtime keys before crawler imports (`runtime_env.py`),
  so UI-set keys take effect on a `restart`, no rebuild.

## When Docker / WSL won't start

This machine has a **recurring** Docker-Desktop-on-WSL2 failure (WSLService getting
disabled; un-deletable orphaned AF_UNIX socket reparse points crash-looping the engine).
The full diagnosis and the installed auto-fix (`%LOCALAPPDATA%\DockerCleanStart\clean-start.ps1`,
plus the CCleaner root-cause and registry-audit backstop) are recorded in the
**`env-docker-wsl`** memory — read that before improvising. Quick triage: `Get-Service
WSLService` must be Running/Manual; if the engine crash-loops on "initializing
Inference/Secrets manager … cannot be accessed", fully stop Docker and **rename** the stale
`%LOCALAPPDATA%\Docker\run` / `%LOCALAPPDATA%\docker-secrets-engine` folders aside (rename,
never delete — they're un-deletable), then relaunch.

## Verifying a change actually works

Run the `tests/` smoke suite (`pytest`) against the running stack as a first regression
net, then drive the specific path: `curl` a backend route (`/docs` lists them), hit the
module list at `/api/shell/modules`, or open the UI and exercise the flow (the `run` /
`verify` skills). Don't trust a diff alone — a mounted edit that wasn't restarted is the most common "why
didn't my change work" cause.

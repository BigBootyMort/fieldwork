# Fieldwork / Runi Shell

Single-user, localhost-only OSINT + personal-dashboard platform. Docker Compose stack
(WSL2 backend) with a legacy "Fieldwork" investigation app being absorbed into a modular
"Runi Shell". One human user (TheHungryRat); no auth, all ports bind 127.0.0.1.

## Project knowledge base — READ THIS FIRST

Detailed, curated project docs live in `docs/kb/`. Consult them before non-trivial work
instead of re-exploring the codebase:

- `docs/kb/architecture.md` — services, ports, data stores, directory map
- `docs/kb/modules.md` — every shell module: purpose, files, API endpoints
- `docs/kb/llm-engines.md` — Claude API / Claude Code bridge / Ollama resolution chain

**Maintenance protocol (for Claude):** these docs are the project's long-term memory.
After completing any change that adds/removes a module, endpoint, service, port, env var,
or architectural decision, update the relevant `docs/kb/` file and, if the top-level shape
changed, this file. Keep entries factual and dated where useful. Prefer editing existing
sections over appending duplicates.

## Layout (top level)

- `shell/` — **active development.** Runi Shell: FastAPI backend (`shell/backend/app`,
  port 8002) + nginx SPA (`shell/frontend`, port 3001). Modules under
  `shell/backend/app/modules/{news,markets,agent,gigs,presence,reports,fieldwork}` with
  matching frontends in `shell/frontend/modules/`.
- `shell/host-bridge/` — Claude Code bridge shim; runs on the **host** (not Docker), port 8088.
- `backend/`, `frontend/` — legacy Fieldwork app (ports 8000 / 3000), still live, loaded
  into the shell as an iframe module.
- `spiderfoot/`, `maigret/`, `theharvester/`, `recon/`, `neo4j/` — supporting OSINT
  containers / DB init.
- `kali-mcp/` — Kali Linux MCP server exposed via the Docker MCP Toolkit (`docker mcp`).
  Not in the compose stack; the toolkit gateway spawns it on demand. See
  `docs/kb/architecture.md` and `kali-mcp/README.md`.
- `NEW/` — old standalone prototype (own docker-compose); not part of the running stack.
- `app/`, `fieldwork/`, `cd/`, `crawlers/`, `CUsersTheHungryRat…*` — empty or junk
  directories (the mangled ones came from a path bug); safe to ignore.
- `AERIS10_BUILD_PLAN.md` — unrelated hardware side project (10.5 GHz radar), not code.

## Conventions & gotchas

- New features = **native shell modules** (recipe: backend package with `ModuleManifest`,
  register in `main.py`, frontend `manifest.js` + view, script tag in `index.html`). nginx
  needs no change — `shell/frontend/nginx.conf` has a catch-all `/api/` proxy. The
  `.claude/skills/runi-shell-module` skill has the full code-level walkthrough.
- Rebuild after changes: `docker compose up -d --build shell-backend shell-frontend`.
- Tests: `tests/` is a live-stack pytest smoke suite (needs the stack up). `pytest` for the
  fast set, `pytest -m slow` for the end-to-end AI paths. Add an assertion when you add an
  endpoint/module. There are no unit tests.
- Neo4j is the single shared datastore (Fieldwork graph + news/report/audit nodes).
- `neo4j/init.cypher` must stay **LF**; repo uses `core.autocrlf input`.
- Backend module files are UTF-8; edit with UTF-8 tools (PowerShell 5.1 defaults to
  UTF-16/cp1252 — pass `-Encoding utf8`).
- Docker Desktop/WSL quirks: WSLService can end up disabled; orphaned AF_UNIX sockets
  crash-loop the engine (see user memory `env_docker_wsl`).
- Commit style: `Module: short description` (e.g. `News: media lean/ownership + …`).
- `ruvector.db` files scattered around are Ruflo plugin artifacts, not app data.

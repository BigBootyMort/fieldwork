---
name: runi-shell-module
description: >-
  Build or modify a native Runi Shell module — the modular dashboard where all NEW
  Fieldwork platform work lives (backend FastAPI at shell/backend/app, nginx SPA at
  shell/frontend, port 3001). Use this skill whenever the task is to add a new module/tab
  to the shell (a dashboard, feed, tool, or panel alongside News, Markets, Agent, Gigs,
  Presence, Reports), add or change an endpoint on an existing shell module, wire a module
  into the nav, or debug why a module isn't loading/registering. Triggers include
  "add a shell module", "new tab in the shell", "ModuleManifest", "Shell.register",
  "shell/backend/app/modules/…", "register the module", or building any feature that isn't
  part of the legacy Fieldwork investigation app (which is a separate single-file frontend
  — use fieldwork-frontend for that).
---

# Add / modify a Runi Shell module

The shell is a small module system: a FastAPI backend (`shell/backend/app`, container port
8002) and an nginx-served SPA (`shell/frontend`, port 3001) that lazy-loads each module.
A module is a backend package + a matching frontend folder, tied together by an id.

**Read one existing module end-to-end first** as a live template — `gigs` is the smallest:
`shell/backend/app/modules/gigs/` and `shell/frontend/modules/gigs/`. Mirror its structure.

## The id is the contract

Pick a lowercase `<id>` (e.g. `calendar`). It appears in: the backend package name, the
manifest `id`, the route prefix `/api/<id>`, the frontend folder, the `Shell.register` id,
and the `window.<Name>View` global. Keep them consistent and everything lines up.

## Backend (2 files under `shell/backend/app/modules/<id>/`)

**`__init__.py`** — declare the manifest and an `init` that returns the router:

```python
from registry import ModuleManifest
from .routes  import build_router

def init(app, deps):
    return build_router(deps)

manifest = ModuleManifest(
    id="calendar",
    label="Calendar",
    icon="📅",
    version="1.0.0",
    prefix="/api/calendar",   # routes mount here
    kind="native",            # "native" (own UI) or "iframe"
    description="…",
    init=init,
)
```

**`routes.py`** — `build_router(deps: dict) -> APIRouter`. Pull what you need from `deps`
(the shared dependency bag built in `deps.py`) and hang routes off a local `APIRouter`:

```python
from fastapi import APIRouter

def build_router(deps: dict) -> APIRouter:
    http     = deps["http"]      # shared httpx.AsyncClient
    settings = deps["settings"]  # env-backed Settings
    graph    = deps["graph_db"]  # async neo4j driver
    audit    = deps["audit"]     # writes AuditLog nodes
    bus      = deps["bus"]       # in-process EventBus for cross-module events

    router = APIRouter()

    @router.get("/list")         # exposed as GET /api/calendar/list
    async def list_items():
        ...
    return router
```

Route paths are relative to the manifest `prefix`. For LLM calls use the shared
`llm_bridge` (`claude_complete()` → `(text, engine)`, raises `NoClaudeError`) and degrade to
Ollama — see `docs/kb/llm-engines.md`. Persist to the shared Neo4j via `graph_db`.

**Register in `shell/backend/app/main.py`** (grep `registry.register`): add
`from modules.calendar import manifest as calendar_manifest` next to the others and
`registry.register(calendar_manifest)`. This import-time registration is what makes the
module appear in `/api/shell/modules` (the nav source).

## Frontend (folder `shell/frontend/modules/<id>/`)

Four files, following the lazy-load convention so a module's JS/CSS only loads when opened:

- **`manifest.js`** — calls `Shell.register({...})` with `mount(root)` and `unmount()`.
  `mount` injects the stylesheet once, lazy-loads `view.js` (which sets
  `window.<Name>View`), then calls `window.<Name>View.mount(root)`. Optionally add a
  `palette` array of command-palette actions. Copy `gigs/manifest.js` and rename.
- **`view.js`** — `window.<Name>View = (() => { … return { mount, unmount }; })();`
  `mount(root)` typically `fetch`es `view.html` into `root.innerHTML`, binds events, and
  kicks off data loading. Use `Shell.api(path)` / `fetch('/api/<id>/…')` for backend calls,
  `Shell.emit`/bus for cross-module events, `Shell.switch(id)` to navigate.
  **Watch the IIFE boundary**: a `return { mount, unmount }` must be the *last* statement —
  a stray early return silently turns everything after it into dead code (this exact bug
  disabled several Markets tabs). Run `node --check view.js` after structural edits.
- **`view.html`** — the module markup fetched by `mount`.
- **`style.css`** — module styles (namespaced by a module class prefix).

**Add the script tag** to `shell/frontend/index.html` (grep `modules/`):
`<script src="/modules/<id>/manifest.js"></script>`.

## nginx — nothing to do

`shell/frontend/nginx.conf` proxies **all** `/api/` to the backend with one catch-all
`location /api/` (300s timeouts for slow LLM calls). Adding a module does **not** require an
nginx change — the file's own header says so. (Older docs list a per-module proxy step;
it's stale.)

## Deploy & verify

Both `shell/backend/app` and `shell/frontend/{index.html,modules,…}` are **volume-mounted**
into their containers, so no rebuild is needed for ordinary code edits — but the two sides
refresh differently:
- **Frontend** edits are live on a hard refresh (Ctrl+Shift+R) — nginx serves the mounted
  files directly.
- **Backend** uvicorn runs *without* `--reload`, so a mounted edit is on disk in the
  container but the running process won't pick it up until you
  `docker compose restart shell-backend`. (A new module import in `main.py` needs this
  restart too.)
- `docker compose up -d --build shell-backend shell-frontend` is only for changed Python
  dependencies or the image itself.

Confirm the module registered: `curl http://localhost:8002/api/shell/modules` should list
your id; `curl http://localhost:8002/api/<id>/…` should hit a route; then open
http://localhost:3001 and check the nav + panel. Drive it (the `run`/`verify` skills) rather
than trusting the diff.

## After shipping

Per the `CLAUDE.md` maintenance protocol, update `docs/kb/modules.md` (add the module: id,
purpose, files, endpoints) and, if the top-level shape changed, `CLAUDE.md`. These docs are
the project's long-term memory — keep them factual.

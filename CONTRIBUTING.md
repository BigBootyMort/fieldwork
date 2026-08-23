# Contributing to Runi-OS

Thanks for your interest. Runi-OS is a single-operator, localhost-first platform; these
notes cover how to run it, where things live, and the conventions that keep it coherent.

## Run the stack

```bash
cp .env.example .env      # set NEO4J_PASSWORD (openssl rand -hex 24)
docker compose up --build # 16 services, all bound to 127.0.0.1
```

Main entry: http://localhost:3001 · Shell API docs: http://localhost:8002/docs

Both `shell/backend/app` and `shell/frontend` are volume-mounted, so ordinary code edits
are live (uvicorn reload / hard refresh). Rebuild only when a dependency or image changes:
`docker compose up -d --build shell-backend shell-frontend`.

## Layout

- `shell/` — the modular Runi Shell (active development): FastAPI backend + nginx SPA.
- `backend/`, `frontend/` — the legacy engine being absorbed (Strangler Fig).
- `eval/` — dependency-free harness scoring the AI synthesis layer (see `eval/README.md`).
- `docs/` — architecture, modules, and the demo/screenshot guide.

## Adding a shell module

A feature is a self-contained module: a backend package with a `ModuleManifest` registered
in `shell/backend/app/main.py`, plus a frontend `manifest.js` + view. The nginx catch-all
`/api/` proxy means no web-server config change is needed to expose a new route. See
`docs/kb/modules.md` for the full recipe.

## Conventions

- **Commits:** `Area: short description` (e.g. `News: add media-lean scoring`).
- **Encoding:** backend files are UTF-8 — edit with UTF-8-aware tools.
- **`neo4j/init.cypher` must stay LF.**
- **Docs are the long-term memory:** after a change that adds/removes a module, endpoint,
  service, or env var, update the relevant file under `docs/kb/`.
- **Tests:** `tests/` is a live-stack smoke suite (needs the stack up). The `eval/`
  selftest is standalone (`python eval/run_eval.py --selftest`) and runs in CI.

## Before opening a PR

- [ ] `python eval/run_eval.py --selftest` passes.
- [ ] `python -m compileall backend/app shell/backend/app eval` is clean.
- [ ] No secrets or personal data added (see `PUBLISH_CHECKLIST.md`).

## Scope & ethics

Runi-OS is for **authorized** OSINT and investigation on data you have the right to
process. It ships without authentication and is intended for localhost use only — do not
deploy it publicly without adding auth, TLS, and rate limiting.

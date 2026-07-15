# Smoke tests

Integration smoke tests that drive the **running** docker-compose stack over HTTP. They
catch the kind of regressions this project is prone to — a module dropping out of the
registry, a crawler starting to 500 instead of degrading gracefully, type-detection
breaking, the AI pipeline falling over. They are intentionally shallow and data-agnostic
(assert shapes, not specific values) so they stay green as real data changes.

There is no in-process app import here — the apps pull in Neo4j/etc., so testing the live
stack is simpler and matches how the system actually runs.

## Setup (once)

```bash
python -m pip install -r tests/requirements-dev.txt
```

## Run

Bring the stack up first (`docker compose up -d`), then from the repo root:

```bash
pytest                 # fast smoke — reachability, module registry, type detection,
                       # graceful degradation, endpoint shapes (excludes slow/LLM)
pytest -m slow         # end-to-end AI: news brief + orchestrated investigation
pytest -m ""           # everything
pytest -m "not network"  # skip anything that calls a third-party API
```

If the stack isn't reachable, the whole suite **skips** (not fails) with a message telling
you to start it.

## Layout

- `conftest.py` — env-overridable base URLs, shared HTTP client, stack-reachable guard.
- `test_stack_health.py` — frontends serve, legacy `/docs`, shell module registry intact.
- `test_shell_modules.py` — news (`llm-status`/`sources`/`heatmap`/`articles`) + markets
  `indicators` shapes.
- `test_legacy_api.py` — `investigate/detect` type detection + crawler graceful-degradation.
- `test_ai_optional.py` — `slow`: news brief + `investigate/orchestrate` end-to-end.

## Markers

- `slow` — LLM / heavy endpoints; excluded by default (see `pytest.ini`).
- `network` — depends on a third-party API (Yahoo, ipinfo); skips gracefully if the upstream
  is down.

Base URLs override via env: `SHELL_API`, `LEGACY_API`, `SHELL_WEB`, `LEGACY_WEB`.

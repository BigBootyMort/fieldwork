---
name: verify-fieldwork
description: >-
  Smoke-test playbook for the Fieldwork platform — how to prove a change actually works
  when there is NO automated test suite in this repo. Use this skill after editing any
  backend route, crawler, shell module, frontend view, the graph layer, or the AI
  investigation engine, and before declaring a change done. Also use it to sanity-check the
  whole stack is healthy ("is everything working?", "did my change break anything?"). It
  gives the exact curl checks and UI flows per subsystem (shell, legacy backend, news,
  markets, investigation, graph), and the browser-driving gotchas. Triggers include "verify
  / smoke test / does this work", "test my change", "check the app", "is the stack healthy",
  or finishing any code edit that has a runtime surface. Pair with fieldwork-stack-ops (to
  get the stack up and to know restart vs refresh) and the built-in run/verify skills.
---

# Verifying Fieldwork changes

**There is no pytest/jest suite in this repo** — verification means driving the running
stack and observing real behavior. A green diff proves nothing here; the single most common
"why didn't my change work" is a shell-backend edit that wasn't restarted (see
`fieldwork-stack-ops` for the reload matrix). Always exercise the actual path you changed.

Ports: shell **3001** (main UI), shell-backend **8002**, legacy frontend 3000, legacy
backend **8000**, neo4j 7474/7687. Everything binds `127.0.0.1`.

## 0. Is the stack up?

```bash
docker compose ps                                   # services Up?
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3001/            # 200 = shell
curl -s http://localhost:8002/api/shell/modules | jq '.[].id'              # module list
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/docs        # 200 = legacy API
```

If a backend is up but a route 404s, confirm the reload/restart: legacy backend hot-reloads
on save; **shell-backend does not** — `docker compose restart shell-backend`.

## 1. Pick the checks for what you touched

**A crawler / `/enrich` route (legacy):** curl it directly — good input returns data, bad/
empty/private input returns a clean `{found:false, reason:…}`, missing-key returns a friendly
reason (not a 500):
```bash
curl -s 'http://localhost:8000/enrich/ip/8.8.8.8/ipinfo' | jq
curl -s 'http://localhost:8000/enrich/ip/10.0.0.1/ipinfo' | jq '.found,.reason'   # graceful
```

**A shell module route:** `curl localhost:8002/api/<id>/<route>`; confirm it's in
`/api/shell/modules`. Then open the tab at 3001 and exercise it.

**News:** poll → list → heatmap → brief (poll is async; give it a few seconds):
```bash
curl -s -XPOST http://localhost:8002/api/news/poll
curl -s 'http://localhost:8002/api/news/articles?window=12' | jq '.articles|length'
curl -s 'http://localhost:8002/api/news/heatmap?window=12'  | jq '.points[0]'
curl -s http://localhost:8002/api/news/llm-status | jq '{engine,claude}'   # which LLM
```
A non-zero article count and heatmap points = ingest + scoring work. `llm-status.engine` =
`ollama` means no Claude is configured for the shell (brief quality will be lower).

**Markets:** `GET /api/markets/indicators?symbol=AAPL` returns a `ratios` block + `tech_score`;
open the tab and click through Watchlist / Portfolio / AI Analysis / Screener / Account /
Deep Research — each pane must render its own content (regression guard: a mis-scoped tab
falls through to the portfolio pane).

**Investigation engine:** detect is instant; orchestrate runs tools + a brief:
```bash
curl -s 'http://localhost:8000/investigate/detect?target=tesla.com' | jq
curl -s -XPOST http://localhost:8000/investigate/orchestrate \
  -H 'Content-Type: application/json' -d '{"target":"tesla.com"}' \
  | jq '{type,engine,tools_run,brief_len:(.brief|length)}'
```
Check `engine` is `claude`/`claude-code` (not `ollama`) if you expect Claude, that
`tools_run` is non-empty, and the brief has the mandated sections.

**Graph / persistence:** after a persisted write, read it back — the graph is the source of
truth:
```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  'MATCH (n) RETURN labels(n)[0] AS label, count(*) ORDER BY count(*) DESC LIMIT 10;'
```
Add `"persist":true` to an orchestrate call, then confirm the `Investigation` node and
derived nodes exist with provenance (`via`/`confidence`/`found_at`).

## 2. Driving the UI (browser)

- Prefer `read_page` / `get_page_text` over screenshots to verify text and structure —
  they're reliable and fast.
- **Screenshots of the News tab can time out**: the Leaflet map (external tiles + lib) can
  stall the renderer, especially if the network to the CDNs is slow/blocked. Don't treat a
  screenshot timeout as "the app is broken" — check `read_console_messages` for real errors
  and fall back to DOM reads. (This fragility is itself a known issue — the map pulls
  Leaflet, world GeoJSON, and tiles from external CDNs.)
- Check `read_console_messages(onlyErrors:true)` after interacting — silent JS errors are the
  usual cause of a dead button.
- Hard-refresh (Ctrl+Shift+R) after a frontend edit; nginx sends no-cache but the browser
  may hold a parsed module.

## 3. Before saying "done"

- The exact path you changed was exercised end-to-end, not just adjacent code.
- Error/empty/missing-key paths degrade gracefully (no 500s, no blank panes).
- No new console errors; `node --check` passes on any structurally-edited `view.js`.
- If behavior depends on Claude, you confirmed the engine actually used (don't assume the
  API key is wired — check `llm-status` / the `engine` field).
- Docs updated if you changed an endpoint/module/schema (the `CLAUDE.md` protocol).

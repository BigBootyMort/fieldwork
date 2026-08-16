# Architecture

_Last verified: 2026-07-11_

Two generations coexist in one docker-compose stack:

1. **Legacy Fieldwork** — monolithic OSINT investigation app (~10k LOC Python backend,
   ~18k LOC HTML/JS frontend). Still the workhorse for graph investigations.
2. **Runi Shell** — modular chrome hosting Fieldwork (as an iframe) plus native modules
   (News, Markets, Agent, Gigs, Presence, Reports). All new work goes here.

## Services & ports (docker-compose.yml, all 127.0.0.1)

| Service | Container | Port | Role |
|---|---|---|---|
| neo4j | fieldwork-neo4j | 7474 / 7687 | Shared graph DB for everything |
| neo4j-init | fieldwork-neo4j-init | — | Runs `neo4j/init.cypher` (must be LF) once |
| backend | fieldwork-backend | 8000 | Legacy Fieldwork FastAPI (graph, crawlers, SpiderFoot proxy) |
| frontend | fieldwork-frontend | 3000 | Legacy Fieldwork SPA |
| shell-backend | runi-shell-backend | 8002 | Runi Shell FastAPI (`/api/shell/*` + module routes) |
| shell-frontend | runi-shell-frontend | 3001 | Shell SPA (nginx serves + proxies `/api/*`) |
| spiderfoot | fieldwork-spiderfoot | 5001 | OSINT scanner, built from v4.0 source w/ pyyaml patch |
| maigret / theharvester / recon | fieldwork-* | — | Aux OSINT tools |
| mailaccess | fieldwork-mailaccess | — (internal :8000) | Email OSINT sweep (2500+ platforms, breach detection, identity clustering); ships its own REST server, backend reaches it at `MAILACCESS_URL=http://mailaccess:8000`, drives `POST /api/investigate` → poll `GET /api/report/{id}`. Volume `mailaccess_data` persists its SQLite cache. Route: `/enrich/email/{email}/mailaccess`. |
| ollama | fieldwork-ollama | 11434 | Local LLM fallback (default model llama3.2) |
| ollama-init | fieldwork-ollama-init | — | Pulls models on first start |
| libretranslate | fieldwork-libretranslate | 5000 | Translation for foreign-language news |
| _(host process)_ | claude bridge | 8088 | `shell/host-bridge/claude_bridge.py`, wraps `claude -p` — NOT in Docker |

Main entry point: **http://localhost:3001** (shell). Legacy direct: 3000.
Shell API docs: http://localhost:8002/docs. Module registry JSON: `/api/shell/modules`.

## Shell backend structure (`shell/backend/app/`)

- `main.py` — FastAPI bootloader; registers all module manifests at import time;
  lifespan builds shared deps. Shell-level routes: `/api/shell/health`, `/modules`, `/config`.
- `registry.py` — `ModuleRegistry` + `ModuleManifest` (id, label, icon, prefix, kind
  native|iframe, `init(app, deps) -> APIRouter`).
- `deps.py` — shared deps dict: `settings` (env-backed `Settings`), `graph_db`
  (async neo4j driver), `http` (shared httpx client), `audit` (writes `AuditLog` nodes),
  `bus` (in-process pub/sub `EventBus` for cross-module events).
- `llm_bridge.py` — shared Claude access layer (see `llm-engines.md`).
- `modules/<id>/` — one package per module (see `modules.md`).

Key `Settings` env vars: `NEO4J_URI/USER/PASSWORD`, `FIELDWORK_API` (http://backend:8000),
`FIELDWORK_FRONT` (http://localhost:3000), `OLLAMA_URL/MODEL`, `GITHUB_TOKEN`,
`ALPACA_API_KEY/SECRET/BASE_URL` (defaults to paper-api), `AGENT_WORKSPACE`.

## Shell frontend (`shell/frontend/`)

- `index.html` — chrome (header, nav, root, command palette) + one `<script>` tag per
  module manifest.
- `shell.js` — `Shell` global: module registry/`register()`, view switching,
  event bus (`Shell.emit`), `Shell.api()`, toasts, palette.
- `nginx.conf` — serves SPA and proxies **all** `/api/` to shell-backend:8002 via one
  catch-all `location /api/` (300s timeouts for slow LLM calls). Adding a module needs **no**
  nginx change — a newly registered `/api/<id>/` route is reachable immediately.
- `modules/<id>/` — `manifest.js` (registers with Shell), `view.html`, `view.js`, `style.css`.

**No external CDNs (both frontends).** As a localhost-only, OPSEC-sensitive tool, all
third-party assets are vendored and served by nginx — nothing loads from a CDN at runtime.
Shell: `modules/news/vendor/` (Leaflet + world GeoJSON), `modules/vendor/fonts/` (web fonts).
Legacy: `frontend/static/vendor/` (Leaflet + plugins, Cytoscape, world GeoJSON),
`frontend/static/fonts/`. Maps render a local world-outline GeoJSON instead of external map
tiles. Only opt-in `target="_blank"` links (OSM/Google Maps "view" links) hit external hosts,
and only on user click.

## Data model (Neo4j, single instance shared by everything)

- Fieldwork-managed: `Person`, `Company`, `Location`; SpiderFoot-promoted: `Email`,
  `Phone`, `Username`, `Account`, `IP`, `Domain`, `Breach`, `Leak`, `Wallet`.
  Relationships carry provenance (`source`, `module`, `first_seen`).
- SpiderFoot event→node mapping: `backend/app/spiderfoot_client.py` `EVENT_MAP`
  (~25 high-value types of ~200).
- Shell additions: `NewsArticle` (now carries an `embedding` array for topic
  classification + story clustering), `NewsCountry` (news), `ReportDoc` (reports),
  `AuditLog` (deps.audit).
- Orchestrator persistence (from `graph_intel.persist_investigation`): an
  `Investigation` node per run linked `INVESTIGATED`→ the target, plus derived nodes
  `Subdomain`, `Nameserver`, `Organization`, `Email`, `Breach`, `Sanction`, `CourtCase`,
  `ThreatPulse`, `Subreddit`, `Attribute`, `CryptoAddress`. Every edge records
  `via` (tool) + `confidence` + `found_at`.

## Legacy Fieldwork API (port 8000) highlights

Graph/crawl: `/search/person`, `/crawl/person` (2-hop subgraph), `/paths`,
`/person/{id}`, `/person/{id}/sources`, `/spiderfoot/*`
(startscan/status/results/scans/promote). SpiderFoot scan profiles: **passive**
(default, no target contact), investigate, footprint, all — never use footprint/all
for OPSEC-sensitive targets.

### AI investigation layer (added 2026-07; modules in `backend/app/`)

- **`orchestrator.py`** — one target → auto-detect type (name/email/domain/ip/company/
  username/**phone**/crypto_eth) → concurrent multi-tool fan-out → Claude-synthesised brief.
  `POST /investigate/orchestrate` (`persist:true` writes to the graph, `case_id` opt).
  `deep_investigate()` = recursive BFS auto-pivot.
  - _2026-07-28:_ widened the fan-out (`_tasks_for`) to use more of the crawler
    arsenal — ip:+Shodan/Censys/VirusTotal, domain:+VirusTotal/Wayback,
    email/username:+Dehashed/GoogleDorks, name/company:+Aleph(OCCRP)/GoogleDorks,
    crypto:+Arkham; added a **`phone`** type (phone_intel + Dehashed + dorks).
  - Every run now returns a **`coverage`** block classifying each tool as
    `data / no_findings / blind (key not set) / failed`, with reasons. It's fed
    into the synthesis prompt so confidence tracks what was actually collected and
    blind sources aren't reported as negative findings.
  - **Relevance gate** (`relevance.py`, pure-stdlib): keyword crawlers
    (Court Records, Adverse Media, Reddit search) routinely return items about
    *other* people; `apply_relevance()` demotes items whose text doesn't mention
    the target into a `weak_matches` bucket and recomputes `found`/`total` so the
    brief can't launder them as confirmed facts. Applied for name/company targets;
    the synth prompt is told to treat `weak_matches` as unverified. Unit-tested in
    `tests/test_relevance.py` (no stack needed).
  - `github.py`, `sec.py`, `opencorporates.py` now expose read-only dict-returning
    variants (`search_github_users`, `search_sec_filings`, `search_opencorporates`)
    wired into the name/company fan-out — alongside the write-only class crawlers
    the legacy person-crawl pipeline still uses. `maigret` (container) not yet wired.
  - _Residual recall gap:_ `court_records.py`'s CourtListener query can miss a
    target's actual landmark case (e.g. the SDNY criminal docket) — a precision fix
    (relevance gate) is in; recall tuning is a separate follow-up.
- **`graph_intel.py`** — `persist_investigation()` writes findings to Neo4j with
  provenance; `get_investigation_subgraph()` (`GET /investigate/graph?target_id=`);
  `merge_entities()` uses APOC `mergeNodes` (`POST /investigate/merge`).
- **`link_analysis.py`** — shared-entity + duplicate detection across investigations +
  Claude narrative. `POST /investigate/link-analysis`.
- **`inv_monitor.py`** — scheduled re-investigation + finding-diff alerts (registered on
  the existing APScheduler). `GET|POST|DELETE /monitor/investigations`, `/{id}/run`,
  `GET /monitor/alerts`. State in `backend/app/inv_monitors.json`.
- **`vision_intel.py`** — `POST /analyze/image/intel`: EXIF + Claude-vision geolocation
  (API-only, see `llm-engines.md`).
- **`llm.py`** — case summaries / hypotheses / entity extraction, Claude-routed via the
  Fieldwork `llm_bridge`.

New enrichment crawlers (in `backend/app/crawlers/`): Companies House, AlienVault OTX,
Reddit, Wikidata, Etherscan, maritime/AIS, Nominatim geocode — endpoints under
`/enrich/…`. Keys live in `backend/app/runtime_api_keys.json` (gitignored).

## Kali MCP server (`kali-mcp/`, via Docker MCP Toolkit)

Standalone MCP server exposing curated Kali Linux recon/scan tools to MCP clients
(Claude Code, etc.). **Not** part of the docker-compose stack — managed by the
Docker Desktop **MCP Toolkit** (`docker mcp`), spawned on demand by its gateway.

- `kali-mcp/Dockerfile` — `kalilinux/kali-rolling` + curated tools (nmap, dnsutils,
  whois, nikto, whatweb, gobuster, sslscan, wafw00f, dnsrecon, wordlists) + Python
  venv. Image tag `kali-mcp:latest` (~800 MB).
- `kali-mcp/server.py` — FastMCP (stdio) server. Tools: `nmap_scan`, `dns_lookup`,
  `whois_lookup`, `whatweb_scan`, `nikto_scan`, `gobuster_dir`, and an allowlisted
  generic `run_command` (allowlist extendable via `KALI_MCP_ALLOWLIST` env var).
  Authorized-use scoping is baked into the tool/server descriptions.
- `kali-mcp/catalog.yaml` — Docker MCP catalog entry pointing at the image.

Toolkit wiring (already applied): custom catalog `kali-catalog:latest` → profile
`kali` → `claude-code` client connected. Claude Code launches the gateway via
`docker mcp gateway run --profile kali` (see repo `.mcp.json`, `MCP_DOCKER` server).
Rebuild + re-register after server changes:
`docker build -t kali-mcp:latest kali-mcp/` (the profile references the tag, so a
rebuild is picked up on the next gateway spawn). Full recipe in `kali-mcp/README.md`.

## Dead / inert paths

- `NEW/` — early standalone prototype (own compose, graph.py, opencorporates.py); superseded.
- `app/`, `fieldwork/`, `cd/`, `crawlers/` — empty.
- `CUsersTheHungryRatfieldworkshell…` dirs at root — junk from a path-separator bug.
- `maltego_transforms.py` — standalone Maltego integration script at repo root.
- `ruvector.db` copies (root, shell/frontend, module dirs, host-bridge) — Ruflo plugin
  artifacts, not app data.

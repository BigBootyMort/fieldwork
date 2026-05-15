# Fieldwork engine — Docker stack

The graph database + FastAPI backend + SpiderFoot OSINT engine that the
**Fieldwork** HTML app talks to for its "Network graph" view.

> Single-user, localhost-only by design. All ports bind to `127.0.0.1`.
> Do not expose to a public network without adding auth.

## What's in here

- **Neo4j 5** — graph database. Stores Person, Company, Email, Phone,
  Username, Account, IP, Domain, Breach, Wallet, Location nodes.
- **FastAPI backend** (port 8000) — REST API the HTML app talks to. Owns the
  graph, runs crawlers, proxies SpiderFoot.
- **OpenCorporates crawler** — corporate registries → board memberships.
- **SpiderFoot** (port 5001) — 200+ OSINT modules. Built from v4.0 release
  source with workarounds for known Python 3.11 build issues. See notes below.

## First-time setup

```bash
# 1. Copy the env template and edit it
cp .env.example .env
#    Generate a real password:
openssl rand -hex 24
#    Paste it as NEO4J_PASSWORD in .env

# 2. (Optional) Add API tokens for crawlers
#    OPENCORPORATES_TOKEN, GITHUB_TOKEN, NEWS_API_KEY
#    Leave blank to disable that crawler

# 3. Start the stack (first build takes 3–5 minutes because SpiderFoot
#    builds from source)
docker compose up -d --build

# 4. Wait until everything is healthy
docker compose ps
# You should see:
#   fieldwork-neo4j        healthy
#   fieldwork-backend      running
#   fieldwork-spiderfoot   running
#   fieldwork-neo4j-init   exited (0)

# 5. Open the HTML app and configure
#    Settings → Backend:
#      Engine URL:     http://127.0.0.1:8000
#      SpiderFoot URL: http://127.0.0.1:5001
#    Click each Test button to confirm.
```

## SpiderFoot honest notes

SpiderFoot is bundled because nothing else covers as much ground for free.
But the project is **not actively maintained** — the last open-source release
was v4.0 in April 2022. Practical consequences:

- We build it from source with a small patch (visible in
  `spiderfoot/Dockerfile`) because v4.0's `pyyaml==5.4.1` pin doesn't build on
  modern Python. The patch unpins pyyaml to a 6.x version.
- Some upstream modules' data sources have changed their APIs since 2022.
  Expect ~5-15% of modules to be silently broken.
- Use the **"passive"** scan profile by default. It's the only profile that
  doesn't probe the target directly (DNS lookups, HTTP fetches, port scans).
  For OPSEC-sensitive work, never use "all" or "footprint".

The HTML app's SpiderFoot panel offers four profiles:

| Profile | What it does |
|---|---|
| **passive** | Only data sources that index the target externally. No traffic to target. |
| **investigate** | Passive + malicious-indicator checks (still no direct probes). |
| **footprint** | Active recon: DNS queries, banner grabs, etc. Target will see traffic. |
| **all** | Every module, including the loud ones. |

## Endpoints

### Engine (FastAPI, port 8000)

| Path | Method | Body | Purpose |
|---|---|---|---|
| `/`            | GET  | — | Smoke test |
| `/health`      | GET  | — | Includes DB ping |
| `/search/person`  | POST | `{name, company?}` | Find existing persons |
| `/crawl/person`   | POST | `{name, company?}` | Run crawlers, return 2-hop subgraph |
| `/paths`          | POST | `{source_id, target_id, max_depth}` | Shortest paths |
| `/person/{id}`    | GET  | — | Fetch a person |
| `/person/{id}/sources` | GET | — | Heuristic "potential sources" |
| `/spiderfoot/startscan`         | POST | `{scanname, target, target_type, usecase}` | Start scan |
| `/spiderfoot/scan/{id}/status`  | GET  | — | Scan status |
| `/spiderfoot/scan/{id}/results` | GET  | — | Scan events |
| `/spiderfoot/scans`             | GET  | — | List all scans |
| `/spiderfoot/promote`           | POST | `{scan_id, case_id?, target_node_id?}` | Promote scan results to graph |

### SpiderFoot (port 5001)
Native SpiderFoot web UI. Useful for browsing scan details the app doesn't
surface, or running multi-target scans.

## Schema (Neo4j)

Engine-managed nodes: `Person`, `Company`, `Location`

SpiderFoot-promoted nodes: `Email`, `Phone`, `Username`, `Account`, `IP`,
`Domain`, `Breach`, `Leak`, `Wallet`

Relationships are typed and have provenance properties (`source`, `module`,
`first_seen`).

The mapping from SpiderFoot's ~200 event types to graph nodes is in
`backend/app/spiderfoot_client.py` (`EVENT_MAP`). It deliberately covers only
~25 high-value types. Unmapped events are visible in the SpiderFoot UI but
don't pollute the graph.

## Windows-specific notes

- **Docker Desktop with WSL2 backend** is required.
- **Line endings**: `neo4j/init.cypher` must be saved as **LF** (not CRLF).
  `git config core.autocrlf input` will handle this.
- First build takes longer on Windows due to file-system overhead during the
  SpiderFoot dependency install.

## Logs and debugging

```bash
docker compose logs -f backend       # API + crawler output
docker compose logs -f spiderfoot    # SpiderFoot internals
docker compose logs -f neo4j         # database
```

## What's NOT in here yet

- Additional crawlers: crt.sh, Wayback, GitHub, NewsAPI, OCCRP Aleph
- Putting more API lookups (HIBP, Hunter, etc.) through the engine so results
  become graph nodes
- Sanctions list import (OpenSanctions bulk)

## Resetting

```bash
docker compose down -v   # nukes all data including SpiderFoot scans
```

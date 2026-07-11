# Shell modules

_Last verified: 2026-07-11._ Backend: `shell/backend/app/modules/<id>/`;
frontend: `shell/frontend/modules/<id>/`. All routes mount under `/api/<id>/`.

## fieldwork (iframe)

Wrapper around the legacy app; the shell injects `FIELDWORK_FRONT` as the iframe URL.
Cross-frame messaging via `postMessage` (`shell:event` / `module:event`). No backend routes.

## news — "News & Brief"

Largest native module. RSS ingest from ~11 sources (BBC, Reuters, Guardian, Al Jazeera,
DW, HN, Ars, Krebs, BleepingComputer, The Record… — `sources.py`) → geocoded choropleth
world map + article list + LLM morning brief + Q&A assistant with TTS.

Backend files: `service.py` (ingest, storage, heat scoring
`source_weight × topic_weight × exp(-age_h/12)`, embedding-based topic classification),
`geo.py` (country extraction/mapping), `bias.py` (media lean/ownership + coverage balance
+ neutral AI lean estimation, cached in `bias_ai_cache.json`), `intel.py`
(watchlist/investigation hooks + embedding topic anchors), `llm.py` + `prompts.py`
(brief/ask generation), `watchlist.json` (persisted watchlist). Bias/watchlist/AI-cache
JSON files are gitignored.

Endpoints: `GET /sources`, `POST /poll`, `GET /heatmap`, `GET /articles`,
`GET /llm-status`, `GET /brief`, `POST /ask`, `GET|POST /watchlist`,
`DELETE /watchlist/{id}`, `GET /watchlist/hits`, `GET /stories`, `POST /investigate`
(pivot a story into Fieldwork), `POST /retro`.

Stores `NewsArticle` / `NewsCountry` nodes in Neo4j.

## markets

Stock & crypto dashboard. Equities proxied server-side through Yahoo Finance (CORS);
crypto fetched client-side from CoinGecko. Includes risk/return ratios, composite score,
crypto screener, and LLM analysis of cards. Alpaca **paper trading** integration
(`ALPACA_*` env, defaults to paper-api.alpaca.markets).

`GET /indicators` returns a `ratios` block (1m/3m/6m/1y returns, 30d volatility, Sharpe,
max drawdown, 52w distance, 0-100 `tech_score` + BUY/HOLD/SELL verdict). `GET /screener`
takes `?asset=stocks|crypto` and sorts by tech_score. Watchlist cards are click-to-analyse
(crypto auto-maps `BTC`→`BTC-USD`).

Endpoints: `GET /quote`, `GET /search`, `GET /sparklines`, `GET /indicators`,
`POST /analyze` (LLM), `GET /screener`, `GET /overview`, `GET /alpaca/account`,
`GET /alpaca/positions`, `POST /alpaca/order`, `GET /research/contracts`, `GET /macro`,
`POST /research/analyze`.

Frontend `view.js` is ~108 KB — the heaviest UI in the repo.

## agent

Autonomous multi-step task runner (plan → execute → monitor) with live SSE streaming
and human-in-the-loop interception. Owns `agent_config.json` (in the module dir), which
stores settings shared platform-wide, including `ANTHROPIC_API_KEY` — this file is how
the Agent settings UI configures Claude for every module (see `llm-engines.md`).

Endpoints: `POST /tasks`, `GET /tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/stream` (SSE),
`POST /tasks/{id}/stop`, `POST /tasks/{id}/intercept/respond`, `GET|POST /settings`,
`DELETE /settings/{key}`, `GET /capabilities`, `GET /status`.

## gigs — "Gig Hunter"

Monitors freelance platforms for OSINT/research gigs; LLM-drafted proposals.
Endpoints: `GET /list`, `POST /refresh`, `GET /stats`, `GET /{gig_id}`,
`POST /{gig_id}/draft`, `POST /{gig_id}/status`.

## presence

Content generation for the user's OSINT freelance business: social posts, platform
listings, outreach messages, plus a posting calendar.
Endpoints: `POST /generate/post`, `POST /generate/listing`, `POST /generate/outreach`,
`GET|POST /calendar`, `POST /calendar/{id}/posted`, `DELETE /calendar/{id}`,
`GET /platforms`, `POST /platforms/{platform}`.

## reports

Cross-source intelligence briefings: pulls News nodes + Fieldwork Case/Entity nodes from
the shared Neo4j, generates structured Markdown via the LLM chain, stores `ReportDoc`
nodes. Endpoints: `GET /list`, `GET /generate`, `GET /{report_id}`, `DELETE /{report_id}`.

## Adding a module (checklist)

1. `shell/backend/app/modules/<id>/__init__.py` with `ModuleManifest` + `init()`.
2. Register manifest in `shell/backend/app/main.py`.
3. nginx proxy rule for `/api/<id>/` in `shell/frontend/nginx.conf`.
4. `shell/frontend/modules/<id>/manifest.js` calling `Shell.register({...})` + view files.
5. `<script>` tag in `shell/frontend/index.html`.
6. `docker compose up -d --build shell-backend shell-frontend`.
7. **Update this file.**

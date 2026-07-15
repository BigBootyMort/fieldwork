---
name: osint-investigation
description: >-
  Operate and improve the AI investigation layer of the legacy Fieldwork backend — the
  Claude-synthesised OSINT engine that takes one target, fans out across crawlers, and
  writes an intelligence brief. Use this skill whenever the task is to RUN an investigation
  (orchestrate/deep/link-analysis/merge/monitor a name, email, domain, IP, company,
  username, or ETH address), to interpret or debug its output, or to IMPROVE the Claude
  integration (synthesis prompts, model choice, context budget, tool fan-out, entity
  extraction, case summaries, hypotheses, image geolocation). Triggers include "run an
  investigation / orchestrate a target", "auto-investigate", "deep pivot", "link analysis",
  "why is the brief weak/generic", "make the AI brief better", "tune the OSINT prompt",
  "investigate/orchestrate endpoint", or anything touching orchestrator.py / llm.py /
  vision_intel.py / graph_intel.py. This is the operational + AI-tuning counterpart to
  fieldwork-osint-tool (which adds new data sources).
---

# OSINT investigation engine (operate + improve)

This is the legacy Fieldwork backend's AI layer (port 8000). One target →
auto-detect type → concurrent crawler fan-out → **Claude-synthesised brief** with an
Ollama fallback. Files: `orchestrator.py` (fan-out + synthesis), `graph_intel.py`
(persistence/subgraph/merge), `link_analysis.py`, `inv_monitor.py`, `vision_intel.py`,
`llm.py` (case summaries/entities/hypotheses/chat). All route Claude through the
**Fieldwork** `backend/app/llm_bridge.py` — see `docs/kb/llm-engines.md` for the engine
chain and auth.

## Operating: the endpoints

All under `http://localhost:8000`. The backend runs uvicorn `--reload`, so edits to any of
these files are live on save.

| Call | Body / query | What it does |
|---|---|---|
| `GET /investigate/detect` | `?target=` | Preview the auto-detected type; runs nothing. |
| `POST /investigate/orchestrate` | `{target, type?="auto", persist?=false, case_id?}` | The workhorse: fan-out + one Claude brief. `persist:true` writes findings to Neo4j with provenance. |
| `POST /investigate/deep` | `{target, type?, max_hops(1–2), max_branch(1–6)}` | Recursive BFS auto-pivot (email→domain→people→…), global cap 8 nodes, one brief over the whole expansion. |
| `GET /investigate/graph` | `?target_id=&depth=1` | Knowledge subgraph around a persisted target (for rendering). |
| `POST /investigate/merge` | `{keep_id, merge_id}` | Entity resolution — merges a duplicate node via APOC `mergeNodes`. |
| `POST /investigate/link-analysis` | _(no body)_ | Claude analysis across the whole graph: shared entities (hidden links), duplicate suggestions, top pivots. |
| `…/monitor/investigations` | `GET` list · `POST {target,type?,interval_h=24}` add · `DELETE /{id}` · `POST /{id}/run` | Scheduled re-investigation + change alerts (APScheduler; state in `inv_monitors.json`). First run sets the baseline. |
| `GET /monitor/alerts` | `?limit=` | Finding-diff alerts raised by monitors. |
| `POST /analyze/image/intel` | image upload | EXIF + **Claude-vision** geolocation. |

**Orchestrate response shape** (what the UI and any caller consume):
```json
{ "target": "...", "type": "domain", "tools_run": ["RDAP/WHOIS", ...],
  "engine": "claude|claude-code|ollama", "brief": "## Summary …",
  "results": { "RDAP/WHOIS": { ... }, ... }, "graph": { "nodes": N } }
```
`engine` tells you which LLM actually wrote the brief — check it when a brief looks weak
(`ollama` means no Claude was available). Individual tool results carry `{found, reason}`
or `{error}`; a failed tool degrades gracefully and is noted in the digest, not fatal.

Quick smoke test:
`curl -s -XPOST localhost:8000/investigate/orchestrate -H 'Content-Type: application/json' -d '{"target":"tesla.com"}' | jq '{type,engine,tools_run}'`

## How synthesis works (the pipeline to tune)

1. **Type detect** — `detect_type()` in `orchestrator.py` → one of
   `name|email|domain|ip|company|username|crypto_eth`.
2. **Fan-out** — `_tasks_for(value, ttype, graph_db)` maps each type to a concurrent list of
   crawlers (e.g. domain → RDAP/WHOIS, CertTransparency, PassiveDNS, URLScan, Hunter, OTX,
   HIBP). `_safe()` wraps each so one failure can't sink the batch.
3. **Digest** — `_digest()` compacts raw results into the LLM prompt, **trimming each tool's
   JSON to ~900 chars** and flagging unavailable tools.
4. **Synthesise** — `claude_complete(system=_SYNTH_SYSTEM, user=digest, max_tokens=2000)`;
   on `NoClaudeError` it falls back to Ollama `/api/generate`. `deep_investigate` uses
   `_DEEP_SYSTEM` and synthesises once over all hops (per-hop fan-out runs with
   `synthesize=False`).

The two system prompts (`_SYNTH_SYSTEM`, `_DEEP_SYSTEM`) are string constants at the top of
`orchestrator.py`. `_SYNTH_SYSTEM` fixes the brief's structure — mandatory markdown
sections, `[TOOL]` citation convention, `[CRITICAL]/[HIGH]/[MEDIUM]/[LOW]` severity tags,
and a "don't invent data / classify confirmed-reported-unverified" rule. Editing these
constants changes every brief.

## Improving the Claude integration — the levers

When the ask is "make the AI better", these are the knobs, cheapest first:

- **Prompts** (`_SYNTH_SYSTEM` / `_DEEP_SYSTEM` in `orchestrator.py`; the prompt builders in
  `llm.py`). Most quality wins live here — sharpen the sections, tighten the citation/no-
  fabrication rules, add domain-specific guidance. Change the prompt, re-run the same target,
  compare briefs.
- **Context budget**: the `~900`-char per-tool trim in `_digest` and `max_tokens=2000` in the
  `claude_complete` call. Raising them gives Claude more evidence and room to reason (better
  briefs) at higher token cost — reasonable with a real API key, less so on the rate-limited
  subscription bridge. Tune together and watch for prompt bloat from noisy tools.
- **Tool coverage**: add crawlers to the relevant branch of `_tasks_for` so Claude has more
  to synthesise (adding a *new* source end-to-end is the `fieldwork-osint-tool` skill).
- **Model**: default is `claude-haiku-4-5-20251001` (in `llm_bridge.py`). A stronger model
  (Sonnet/Opus) markedly improves synthesis quality; weigh cost/latency. Respect the
  credential-aware auth (OAuth `sk-ant-oat…` vs api-key `sk-ant-api…`) documented in
  `docs/kb/llm-engines.md` — don't revert the Haiku id to the old one that 404s for OAuth.
- **Engine availability**: briefs are only as good as the engine that wrote them. If
  `engine` comes back `ollama` or synthesis is failing, the fix is usually configuring a
  Claude key, not the prompt. **This layer reads its key from
  `backend/app/runtime_api_keys.json`** (the Fieldwork bridge) — NOT the shell's
  `agent_config.json`. Two separate stores; setting one doesn't populate the other.
- **Vision is API-only**: `/analyze/image/intel` uses `call_claude_api_vision`; the text
  `claude -p` bridge can't see images, so image geolocation needs a real token/key, not just
  the bridge.

## Other AI entry points (`llm.py`, Fieldwork bridge)

`extract_entities(text)`, `summarize_case(bundle)`, `suggest_hypotheses(bundle)`, and
`chat(...)` all funnel through a shared `_generate()` → `claude_complete`. Same prompt-and-
budget levers apply. `summarize_case`/`suggest_hypotheses` take a case *bundle* (notes +
subjects) compacted by `_bundle_to_text` (its own `max_notes`/`max_subjects` caps) — another
context-budget knob.

## Working discipline

- Iterate on prompt/budget changes against a **fixed set of real targets**, re-running the
  same input and diffing the `brief` — synthesis is non-deterministic, so eyeball a couple
  of runs, not one.
- Keep `_digest` output bounded; a single verbose tool can crowd out everything else.
- When persisting, remember every derived node/edge records provenance (`via` tool +
  `confidence` + `found_at`) — preserve that if you touch `graph_intel.persist_investigation`.
- If you add/rename an endpoint or change response shape, update `docs/kb/architecture.md`
  (AI investigation layer section) per the `CLAUDE.md` maintenance protocol.

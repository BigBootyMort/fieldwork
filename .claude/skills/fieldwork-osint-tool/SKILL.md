---
name: fieldwork-osint-tool
description: >-
  End-to-end recipe for adding or modifying an OSINT enrichment tool in the legacy
  Fieldwork app — a new data source that takes an indicator (IP, domain, email, username,
  phone, wallet, name, company) and returns intelligence. Use this skill whenever the task
  is "add a lookup/enrichment/OSINT source", "wire up <some API> (Shodan, VirusTotal,
  Hunter, Censys, an ASN/breach/sanctions/threat-intel provider…)", "add a tool tab", or
  "expose <crawler> in the UI". It covers all three layers: a crawler in
  backend/app/crawlers/, a FastAPI /enrich route in backend/app/main.py, and a tool tab +
  panel + handler in frontend/index.html. Prefer this over ad-hoc wiring so the new tool
  matches the ~45 existing crawlers' conventions (graceful errors, runtime API keys,
  auditing, XSS-safe rendering). Also covers wrapping a vendored Python tool / CLI /
  self-hosted server (MailAccess, maigret, theHarvester, TorBot, horus) as a sibling
  HTTP container so the crawler layer stays a plain API call.
---

# Add an OSINT enrichment tool (full stack)

Most tools span three layers (crawler → route → UI). A tool that is a **Python
package / CLI / self-hosted server** rather than a hosted API adds a **Layer 0** first:
run it as its own container. Build layers in order so each is testable before the next.

Study one existing tool end-to-end first as a live template — `ipinfo` is the cleanest:
`backend/app/crawlers/ipinfo.py`, its route (`grep '/ipinfo' backend/app/main.py`), and its
handler (`grep 'function runIPInfo' frontend/index.html`). Mirror whichever existing tool is
closest to your indicator type.

## Layer 0 — Vendored tool that isn't a hosted API (sibling container)

Skip this if your source is a hosted HTTP API you call directly (most are) — go to Layer 1.

But some tools you want are **Python packages / CLIs / self-hosted servers** (MailAccess,
maigret, theHarvester, TorBot, horus, a recon binary). Do **not** `pip install` these into
the backend image — they drag heavy, conflicting deps (spaCy pins, scrapers, Go tools) and
some run for minutes. Run each as its **own sibling container that exposes a small HTTP
API**, exactly like the existing `maigret` / `theharvester` / `recon` services, and have the
crawler call it over the internal Docker network. The Layer 1 crawler is then identical to
any other HTTP source.

Two flavors:

- **A — thin wrapper you write** (tool is a CLI, e.g. maigret, theHarvester). Mirror
  `maigret/` exactly: a `Dockerfile` that installs the tool + `fastapi`+`uvicorn`, and a
  `server.py` that shells out via `asyncio.create_subprocess_exec`, parses the output, and
  returns a normalized dict. Study `maigret/server.py` — strict input regex, an **outer
  wall-clock timeout**, a `TemporaryDirectory` for output, `{...: [...], "count": n}` back.
- **B — the tool ships its own server** (e.g. MailAccess `mailaccess serve` on :8000). No
  wrapper — just a `Dockerfile` (`pip install <tool>`, `CMD ["<tool>", "serve"]`,
  `EXPOSE <port>`); the crawler talks to the tool's own REST API.

Wiring (both flavors):

1. **`<tool>/Dockerfile`** — copy `maigret/Dockerfile` as the base. It already handles the
   TLS-interception CA trust the build needs (`COPY certs …`; no-ops if `certs/` has no
   `*.pem`). Copy `maigret/certs/avg_root_ca.pem` into `<tool>/certs/` so the build works
   behind the host AV proxy. Run as a non-root user; `EXPOSE` the port.
2. **`docker-compose.yml`** — add a service next to `maigret:` (build context `./<tool>`,
   `container_name: runi-<tool>`, `restart: unless-stopped`, **no `ports:`** so it
   stays internal-only). Add `<TOOL>_URL: http://<tool>:<port>` to the **backend** service's
   `environment:`. If the tool persists state, add a named volume.
3. **health** — if the tool answers `GET /health`, add it to the aggregate `/health` handler
   in `main.py` (grep `_ping(f"{_MAIGRET_URL}/health")`) so the dashboard shows it up/down.
4. Rebuild only that service: `docker compose up -d --build <tool>` (heavy first build —
   these images pull scrapers/models; later builds are cached). The backend picks up the new
   route + URL env without a rebuild (`--reload` + volume mount), but editing its compose
   `environment:` block needs `docker compose up -d backend` to re-read it.

**Long-running / async tools.** A hosted API returns in one call; a full OSINT sweep can
take minutes. If the tool is async (MailAccess: `POST /api/investigate` → `202` + id → poll
`GET /api/report/{id}` until `status` is terminal), the crawler must **start, then poll with
a hard wall-clock cap** (~90–120 s) and degrade to
`{"found": False, "reason": "timed out — still running"}` rather than hang the request. Tools
that cache (MailAccess does) make the retry cheap. Set the `httpx` timeout to match, and
never let it exceed the UI's patience.

## Layer 1 — Crawler (`backend/app/crawlers/<name>.py`)

An async function that does the network call and returns a plain dict. Conventions that
matter:

- Signature: `async def enrich_<thing>_<source>(arg: str) -> dict`.
- Return a **dict with a `found` flag**, never raise for expected failures:
  `{"found": True, ...fields}` on success, `{"found": False, "reason": "<why>"}` for
  not-found / rate-limited / private-input / errors. The whole app relies on this shape;
  callers (routes, auto-enrich, the UI) branch on `found` and show `reason`.
- Use `httpx.AsyncClient(timeout=…)` (10–15s typical). Wrap the request in try/except and
  turn exceptions into `{"found": False, "reason": str(exc)}` after logging a warning.
- Validate/guard input where it matters (e.g. ipinfo skips private/loopback ranges).
- Set a descriptive `User-Agent` (the repo uses `"Fieldwork OSINT"`).

### API keys

Optional keys come from `backend/app/runtime_api_keys.json` (gitignored), which the
Settings UI writes via `_save_runtime_keys`. They're injected into `os.environ` by
`backend/app/runtime_env.py`, which `main.py` imports **before** the crawler imports — so a
UI-set key is present by the time each crawler module runs its top-level `os.getenv`, and
takes effect without a container restart. (This ordering is deliberate: it was previously
broken, and ~14 crawlers that cache their key at module load silently ignored UI-set keys
until a restart.)

Either pattern works, but **reading the key inside the function is the more robust
default** — it can't be defeated by a future import reorder and reflects the current value
on every call:

```python
async def enrich_domain_source(domain: str) -> dict:
    key = os.getenv("SOURCE_API_KEY", "")
    if not key:
        return {"found": False, "reason": "SOURCE_API_KEY not set"}
    ...
```

If you do cache at module level (`_KEY = os.getenv("SOURCE_KEY", "")`), just make sure the
env var name is also what the Settings UI writes, so `runtime_env` can supply it early.

## Layer 2 — Route (`backend/app/main.py`)

1. Add the import next to the other crawler imports at the top (grep `from crawlers`):
   `from crawlers.<name> import enrich_<thing>_<source>`.
2. Add a handler following the enrich-route convention:

```python
@app.get("/enrich/<type>/{arg}/<source>")
async def <type>_<source>(arg: str):
    """One-line description; note required key if any."""
    a = _validate_<type>(arg)          # _validate_ip / _validate_domain where applicable
    res = await enrich_<thing>_<source>(a)
    _audit("<Source>", a, detail=f"…short summary…")
    return res
```

- Path prefix is `/enrich/…`; group by indicator (`/enrich/ip/…`, `/enrich/domain/…`,
  `/enrich/email/…`, `/enrich/wallet/…`, `/enrich/phone/{number:path}` for numbers).
- Use the existing validators (`_validate_ip`, `_validate_domain`, …) so bad input returns
  a clean 4xx instead of hitting the upstream API.
- `_audit(action, subject, detail="", ok=True)` writes an `AuditLog` node — call it so the
  lookup shows in the activity feed. Keep `detail` short and non-sensitive.
- The legacy backend runs uvicorn with `--reload` and `backend/app` is volume-mounted, so
  route edits are picked up on save — no rebuild or restart. Verify with
  `curl 'http://localhost:8000/enrich/ip/8.8.8.8/<source>'`. (This is unlike the *shell*
  backend, which has no `--reload` and needs a restart.)

## Layer 3 — UI (`frontend/index.html`)

This is the legacy single-file frontend — see the `fieldwork-frontend` skill for its
conventions (helpers, `esc()`, theming, editing discipline). To surface the new tool:

1. **Tab button** — add inside `#tools-tabs` (grep `id="tools-tabs"`):
   `<div class="tab" data-tool="<id>">🔎 Label</div>`.
2. **Panel** — add a `<div class="tab-content" id="tools-<id>">` with a `card`, an
   `input-group` (input with a stable `id`), a `btn btn-primary` whose `onclick` calls your
   handler, and an empty results `<div id="<id>-results" style="display:none">`.
3. **Handler** — add `async function run<Thing>()` following the standard shape: read+trim
   input, guard empty, `setLoading`, `apiFetch('/enrich/<type>/<arg>/<source>')`, branch on
   `d.found`, render the fields with **`esc()` on every interpolated value**, `showError` in
   the catch. `encodeURIComponent` the indicator in the URL.

No JS wiring beyond the `data-tool` id + the `onclick` — `switchToolTab` already handles tab
activation generically.

## Definition of done

- `curl` the route: returns `found:true` with data for a good input, and a clean
  `found:false` + `reason` for a bad/empty/private one (not a 500).
- Missing-key case returns a friendly `reason`, not a crash.
- The tab appears, the panel renders, and every rendered field passes through `esc()`.
- If Layer 0 applies: `docker compose up -d --build <tool>` succeeds, `docker compose exec
  backend curl -s http://<tool>:<port>/health` (or the crawler's first call) answers, and
  the tool shows in the aggregate `/health` response.
- If the tool adds a new service, env var, or endpoint, update `docs/kb/` per the
  maintenance protocol in `CLAUDE.md`.

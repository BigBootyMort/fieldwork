# OSINT API keys — what to set to un-blind the crawlers

_Last verified: 2026-07-28._

**Finding (2026-07-28):** in the running stack, **only `ANTHROPIC_API_KEY` had a
value** — every OSINT enrichment key was empty. The orchestrator's fan-out is now
wide, and the `coverage` block reports which sources came back **blind (no key)**,
but the actual *data* only shows up once these keys are set. This is the single
biggest lever on investigation quality.

## Where keys go

Two stores, both work; the orchestrator runs in the **legacy backend** (port 8000):

1. **`backend/app/runtime_api_keys.json`** (recommended, gitignored) — a flat map
   `{ "HUNTER_API_KEY": "…", "GITHUB_TOKEN": "…" }`. `runtime_env.py` injects it into
   `os.environ` **on import, before crawlers cache their keys**. Also settable via the
   Fieldwork Settings UI. Apply with a **backend restart** (no full recreate):
   `docker compose restart backend` (or `up -d backend`).
2. **`.env`** at repo root — compose passes `VAR: ${VAR:-}` to the backend. Single
   source for both backends, but changing `.env` needs a **recreate**:
   `docker compose up -d backend`.

The value guard only fills a var that's currently blank, so `runtime_api_keys.json`
won't override a real `.env`/environment value.

## Priority list (value × availability)

### Tier 1 — free + high impact (set these first)

| Source | Env var(s) | Get it | Un-blinds (target types) |
|---|---|---|---|
| **GitHub** | `GITHUB_TOKEN` | github.com/settings/tokens (classic PAT, no scopes needed) | name, username → repos/orgs |
| **Reddit** | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps → create "script" app | name, username, email |
| **Google Dorks** | `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX` | Programmable Search Engine + API key (100 q/day free) | name, username, email, company |
| **Aleph (OCCRP)** | `ALEPH_API_KEY` | aleph.occrp.org → sign up → profile → API key | name, company (investigative leaks/registries) |
| **AlienVault OTX** | `OTX_API_KEY` | otx.alienvault.com | ip, domain, crypto |
| **urlscan** | `URLSCAN_API_KEY` | urlscan.io free account | domain (more scans, submit) |
| **IPInfo** | `IPINFO_TOKEN` | ipinfo.io (50k/mo free) | ip |
| **GreyNoise** | `GREYNOISE_API_KEY` | greynoise.io community tier | ip |
| **AbuseIPDB** | `ABUSEIPDB_KEY` | abuseipdb.com free tier | ip |
| **Companies House** | `COMPANIES_HOUSE_KEY` | developer.company-information.service.gov.uk | company (UK) |
| **Etherscan** | `ETHERSCAN_API_KEY` | etherscan.io free tier | crypto_eth |
| **EmailRep** | `EMAILREP_KEY` | emailrep.io free tier | email |

_(SEC EDGAR needs **no key** — set a descriptive `SEC_USER_AGENT` and it works.)_

### Tier 2 — freemium / cheap, high impact

| Source | Env var(s) | Cost | Un-blinds |
|---|---|---|---|
| **VirusTotal** | `VIRUSTOTAL_API_KEY` | free public API (rate-limited) | domain, ip |
| **Censys** | `CENSYS_API_ID`, `CENSYS_API_SECRET` | free tier | ip (ports/services) |
| **Hunter** | `HUNTER_API_KEY` | freemium | domain (email pattern/staff) |
| **HaveIBeenPwned** | `HIBP_API_KEY` | paid (low monthly) | email breaches |
| **numverify** | `NUMVERIFY_KEY` | freemium | phone (carrier/line type) |

### Tier 3 — paid, situational

| Source | Env var(s) | Un-blinds |
|---|---|---|
| **Shodan** | `SHODAN_API_KEY` | ip, domain (exposed services) |
| **Dehashed** | `DEHASHED_EMAIL`, `DEHASHED_KEY` | email, username, name, phone (breach records) |
| **Arkham** | `ARKHAM_API_KEY` | crypto (entity attribution) |
| **WHOIS history** | `VIEWDNS_KEY` | domain (historical registrant) |

> Verify current pricing/tiers before signing up — providers change them. Costs
> above are directional, not quotes.

## After setting keys

Restart the backend, then re-run an investigation and check the `coverage` block:
sources should move from `blind` → `data`/`no_findings`.

```bash
docker compose restart backend
```

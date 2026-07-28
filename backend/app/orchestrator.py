"""
AI Investigation Orchestrator — the flagship "strong tool".

Give it one target (name / email / domain / company / IP / username / ETH
address). It auto-detects the type, fans out concurrently across the relevant
crawlers, then has Claude (via the subscription bridge, Ollama fallback)
synthesise a structured intelligence brief from everything collected.

Returns:
  {
    "target", "type", "tools_run": [...], "engine": "claude-code"|...,
    "brief":  "<markdown intelligence brief>",
    "results": { "<tool>": {<raw result>}, ... },
  }
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re

import httpx

from llm_bridge import claude_complete, NoClaudeError
from relevance import apply_relevance

# ── Crawlers (all graceful: missing keys just yield a soft result) ──────────
from crawlers.ipinfo         import enrich_ip_ipinfo
from crawlers.abuseipdb      import check_ip as abuseipdb_check_ip
from crawlers.otx            import enrich_otx
from crawlers.greynoise      import enrich_ip_greynoise
from crawlers.internetdb     import enrich_ip_internetdb
from crawlers.asn            import enrich_ip_asn
from crawlers.rdap           import enrich_domain
from crawlers.whois_history  import get_whois_history
from crawlers.crtsh          import domain_cert_transparency
from crawlers.passive_dns    import passive_dns_domain
from crawlers.urlscan        import search_domain as urlscan_search_domain
from crawlers.hunter         import hunt_domain_emails
from crawlers.hibp           import check_email_hibp, check_domain_hibp
from crawlers.emailrep       import check_email_rep
from crawlers.sanctions      import check_sanctions
from crawlers.court_records  import search_court_records
from crawlers.adverse_media  import search_adverse_media
from crawlers.wikidata       import lookup_wikidata
from crawlers.reddit         import reddit_user, reddit_search
from crawlers.companies_house import search_companies_house
from crawlers.etherscan      import trace_eth_address
# Newly wired standalone crawlers (all graceful: missing keys yield a soft result)
from crawlers.shodan_client  import enrich_ip_shodan
from crawlers.censys         import get_host as censys_get_host
from crawlers.virustotal     import enrich_domain_vt, enrich_ip_vt
from crawlers.wayback        import enrich_domain_wayback
from crawlers.aleph          import search_aleph
from crawlers.dehashed       import search as dehashed_search
from crawlers.google_dorks   import run_dork
from crawlers.arkham         import enrich_wallet_arkham
from crawlers.phone_intel    import enrich_phone
# Read-only variants of the legacy write-only crawlers (return dicts, no graph writes)
from crawlers.github         import search_github_users
from crawlers.sec            import search_sec_filings
from crawlers.opencorporates import search_opencorporates

log = logging.getLogger("fieldwork.orchestrator")

_OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://ollama:11434")
_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

_ETH_RE   = re.compile(r"^0x[a-fA-F0-9]{40}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,}$")
_PHONE_SEP_RE = re.compile(r"[\s().\-]")
_PHONE_RE = re.compile(r"^\+?\d{7,15}$")   # E.164-ish: 7-15 digits, optional +


# ── Target type detection ───────────────────────────────────────────────────

def detect_type(value: str) -> str:
    v = value.strip()
    try:
        ipaddress.ip_address(v)
        return "ip"
    except ValueError:
        pass
    if _ETH_RE.match(v):
        return "crypto_eth"
    if _EMAIL_RE.match(v):
        return "email"
    if _DOMAIN_RE.match(v):
        return "domain"
    # Phone: strip common separators, then match an E.164-ish digit run
    if _PHONE_RE.match(_PHONE_SEP_RE.sub("", v)):
        return "phone"
    # Heuristic: 2+ words of letters → person/company name
    words = v.split()
    if len(words) >= 2 and all(re.match(r"^[A-Za-z.'&,-]+$", w) for w in words):
        # "Ltd/Inc/LLC/Corp/GmbH" → company, else person
        if re.search(r"\b(ltd|inc|llc|corp|gmbh|plc|holdings?|group|co)\b", v, re.I):
            return "company"
        return "name"
    return "username"


_SYNTH_SYSTEM = """\
You are a senior OSINT intelligence analyst. From the structured tool output
provided, write a professional intelligence brief. Be factual and specific —
cite the tool a fact came from in brackets, e.g. [OFAC] or [HIBP]. Do not
invent data; if a section has nothing, say "No findings." Classify claims
(confirmed / reported / unverified) where appropriate.

Ground rules:
- State ONLY what a tool actually reported. Never infer or add facts no tool
  provided (e.g. hosting provider, ASN, employer, affiliation) — if it isn't in
  the tool output, it is not a finding.
- A COLLECTION COVERAGE line tells you how many sources returned findings and
  which were "blind" (not configured) or "failed". Blind/failed sources are
  GAPS, never negative findings — do not write "no breaches" if HIBP was blind.
- Weight overall confidence by the coverage ratio, and list blind/failed
  sources explicitly under Confidence & Gaps.
- Some tools carry a `weak_matches` list and a `relevance` note: those items did
  NOT verifiably mention the target and were demoted. Treat them as unconfirmed —
  do not state them as findings; at most note "unverified possible matches exist."

Use exactly these markdown sections:

## Summary
2-3 sentences: who/what the target is and the single most important finding.

## Identity & Profile
What is established about the target (names, registration, bio, locations).

## Digital Footprint
Domains, IPs, infrastructure, accounts, emails, exposure.

## Risk & Red Flags
Sanctions/PEP hits, data breaches, adverse media, litigation, threat-intel.
Lead each with a severity: [CRITICAL] / [HIGH] / [MEDIUM] / [LOW].

## Connections & Pivots
Linked entities, addresses, counterparties worth pivoting to next.

## Recommended Next Steps
3-5 concrete, specific actions (which tool / source to run next and why).

## Confidence & Gaps
Overall confidence and what data is missing.

Keep it tight and skimmable. No hype words.
"""


# ── Safe concurrent runner ──────────────────────────────────────────────────

async def _safe(name: str, coro) -> tuple[str, dict]:
    try:
        res = await coro
        return name, (res if isinstance(res, dict) else {"value": res})
    except Exception as exc:
        log.debug("orchestrator tool %s failed: %s", name, exc)
        return name, {"error": str(exc)}


async def _aleph(query: str) -> dict:
    """Adapter: search_aleph() returns a bare list and swallows HTTP errors
    (public Aleph 401s without a key), which the coverage classifier can't read.
    Wrap it so a missing key registers as 'blind', not a false 'no findings'."""
    if not os.getenv("ALEPH_API_KEY"):
        return {"found": False,
                "reason": "no key — set ALEPH_API_KEY (public Aleph requires auth)"}
    try:
        res = await search_aleph(query)
    except Exception as exc:
        return {"error": str(exc)}
    return {"found": bool(res), "count": len(res), "entities": res[:20]}


def _tasks_for(value: str, ttype: str, graph_db) -> list[tuple[str, "asyncio.Future"]]:
    """Build the (tool_name, coroutine) fan-out list for a target type.

    Note: the older crawlers take graph_db as their first arg (they persist to
    the graph); the newer standalone ones do not.
    """
    v = value.strip()
    t: list[tuple[str, object]] = []

    if ttype == "ip":
        t += [
            ("IPInfo",     enrich_ip_ipinfo(v)),
            ("AbuseIPDB",  abuseipdb_check_ip(graph_db, v)),
            ("OTX",        enrich_otx(v)),
            ("GreyNoise",  enrich_ip_greynoise(graph_db, v)),
            ("InternetDB", enrich_ip_internetdb(graph_db, v)),
            ("ASN",        enrich_ip_asn(graph_db, v)),
            ("Shodan",     enrich_ip_shodan(graph_db, v)),
            ("Censys",     censys_get_host(v)),
            ("VirusTotal", enrich_ip_vt(graph_db, v)),
        ]
    elif ttype == "domain":
        t += [
            ("RDAP/WHOIS",      enrich_domain(graph_db, v)),
            ("WHOIS History",   get_whois_history(v)),
            ("CertTransparency", domain_cert_transparency(graph_db, v)),
            ("PassiveDNS",      passive_dns_domain(graph_db, v)),
            ("URLScan",         urlscan_search_domain(graph_db, v)),
            ("Hunter",          hunt_domain_emails(v)),
            ("OTX",             enrich_otx(v)),
            ("HIBP (domain)",   check_domain_hibp(graph_db, v)),
            ("VirusTotal",      enrich_domain_vt(graph_db, v)),
            ("Wayback",         enrich_domain_wayback(graph_db, v)),
        ]
    elif ttype == "email":
        domain = v.split("@", 1)[1]
        local  = v.split("@", 1)[0]
        t += [
            ("EmailRep",      check_email_rep(v)),
            ("HIBP",          check_email_hibp(graph_db, v)),
            ("RDAP/WHOIS",    enrich_domain(graph_db, domain)),
            ("Hunter",        hunt_domain_emails(domain)),
            ("Reddit (user)", reddit_user(local)),
            ("Dehashed",      dehashed_search(v, field="email")),
            ("Google Dorks",  run_dork(f'"{v}"')),
        ]
    elif ttype == "name":
        t += [
            ("OFAC/Sanctions", check_sanctions(v)),
            ("Court Records",  search_court_records(v)),
            ("Wikidata",       lookup_wikidata(v)),
            ("Adverse Media",  search_adverse_media(v, _OLLAMA_URL, _OLLAMA_MODEL)),
            ("Reddit (search)", reddit_search(v)),
            ("Aleph (OCCRP)",  _aleph(v)),
            ("SEC EDGAR",      search_sec_filings(v)),
            ("GitHub",         search_github_users(v)),
            ("OpenCorporates", search_opencorporates(v)),
            ("Google Dorks",   run_dork(f'"{v}"')),
        ]
    elif ttype == "company":
        t += [
            ("Companies House", search_companies_house(v)),
            ("OFAC/Sanctions",  check_sanctions(v)),
            ("Court Records",   search_court_records(v)),
            ("Wikidata",        lookup_wikidata(v)),
            ("Adverse Media",   search_adverse_media(v, _OLLAMA_URL, _OLLAMA_MODEL)),
            ("Aleph (OCCRP)",   _aleph(v)),
            ("SEC EDGAR",       search_sec_filings(v)),
            ("OpenCorporates",  search_opencorporates(v)),
            ("Google Dorks",    run_dork(f'"{v}"')),
        ]
    elif ttype == "username":
        t += [
            ("Reddit (user)",   reddit_user(v)),
            ("Reddit (search)", reddit_search(v)),
            ("Wikidata",        lookup_wikidata(v)),
            ("Dehashed",        dehashed_search(v, field="username")),
            ("Google Dorks",    run_dork(f'"{v}"')),
        ]
    elif ttype == "phone":
        t += [
            ("Phone Intel",   enrich_phone(graph_db, v)),
            ("Dehashed",      dehashed_search(v, field="phone")),
            ("Google Dorks",  run_dork(f'"{v}"')),
        ]
    elif ttype == "crypto_eth":
        t += [
            ("Etherscan", trace_eth_address(v)),
            ("OTX",       enrich_otx(v)),
            ("Arkham",    enrich_wallet_arkham(graph_db, v)),
        ]
    return t


# ── Coverage / health telemetry ─────────────────────────────────────────────
# A crawler that can't run because its key is unset is NOT a negative finding —
# it's a blind spot. Distinguish four outcomes so the brief's confidence tracks
# what was actually collected, not just how many tools were dispatched.

_NOKEY_HINTS = (
    "key", "token", "not set", "configure", "credential", "no key",
    "api_id", "api_secret", "quota", "not installed", "not configured",
)


def _has_signal(res: dict) -> bool:
    """True if a clean (error-free) result carries an actual finding."""
    if res.get("found") is True or res.get("valid") is True or res.get("malicious") is True:
        return True
    _META = {"found", "valid", "error", "reason", "note", "query", "target",
             "indicator", "type", "domain", "name", "url", "raw"}
    for k, val in res.items():
        if k in _META:
            continue
        if isinstance(val, (list, dict)) and val:
            return True
        if isinstance(val, (int, float)) and val:
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def _classify(res) -> str:
    """One of: data | no_findings | blind | failed."""
    if not isinstance(res, dict):
        return "data" if res else "no_findings"   # _safe-wrapped list (e.g. Aleph)
    err  = res.get("error")
    hint = f"{err or ''} {res.get('reason', '')} {res.get('note', '')}".lower()
    if err:
        return "blind" if any(h in hint for h in _NOKEY_HINTS) else "failed"
    # graceful soft results: {"found": false, "reason": "no key — …"}
    if (res.get("reason") or res.get("note")) and any(h in hint for h in _NOKEY_HINTS):
        return "blind"
    return "data" if _has_signal(res) else "no_findings"


def _coverage(results: dict) -> dict:
    """Bucket every tool result and record why blind/failed ones produced nothing."""
    buckets = {"data": [], "no_findings": [], "blind": [], "failed": []}
    reasons: dict[str, str] = {}
    for tool, res in results.items():
        cls = _classify(res)
        buckets[cls].append(tool)
        if cls in ("blind", "failed") and isinstance(res, dict):
            why = res.get("error") or res.get("reason") or res.get("note") or ""
            reasons[tool] = str(why)[:140]
    intended = len(results)
    return {
        "intended":    intended,
        "data":        buckets["data"],
        "no_findings": buckets["no_findings"],
        "blind":       buckets["blind"],
        "failed":      buckets["failed"],
        "reasons":     reasons,
        "data_ratio":  round(len(buckets["data"]) / intended, 2) if intended else 0.0,
    }


def _coverage_line(cov: dict) -> str:
    """One compact line stating what was actually collected, for the LLM digest."""
    def j(names): return ", ".join(names) if names else "none"
    return (
        f"COLLECTION COVERAGE: {len(cov['data'])}/{cov['intended']} sources returned "
        f"findings. No findings: {j(cov['no_findings'])}. "
        f"Blind — not configured, treat as GAPS not negatives: {j(cov['blind'])}. "
        f"Failed (transient): {j(cov['failed'])}."
    )


def _digest(value: str, ttype: str, results: dict, coverage: dict) -> str:
    """Compact the raw tool output into LLM context."""
    parts = [f"TARGET: {value}", f"TYPE: {ttype}", "", _coverage_line(coverage),
             "", "TOOL OUTPUT:"]
    for tool, res in results.items():
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            parts.append(f"\n[{tool}] (unavailable: {res['error'][:80]})")
            continue
        # Trim each result to keep the prompt bounded
        blob = json.dumps(res, default=str, ensure_ascii=False)
        if len(blob) > 900:
            blob = blob[:900] + "…"
        parts.append(f"\n[{tool}] {blob}")
    return "\n".join(parts)


async def investigate(value: str, ttype: str = "auto", graph_db=None,
                      synthesize: bool = True) -> dict:
    """Run the full orchestrated investigation and return the brief + raw data.

    synthesize=False skips the LLM brief (fan-out + raw results only) — used by
    deep_investigate so each hop is fast; one brief is written over the whole graph.
    """
    value = (value or "").strip()
    if not value:
        return {"error": "empty target"}
    if ttype in (None, "", "auto"):
        ttype = detect_type(value)

    tasks = _tasks_for(value, ttype, graph_db)
    if not tasks:
        return {"target": value, "type": ttype, "error": f"no tools for type {ttype}"}

    pairs = await asyncio.gather(*[_safe(n, c) for n, c in tasks])
    results = dict(pairs)
    if ttype in ("name", "company"):
        apply_relevance(value, results)    # demote off-target keyword matches
    coverage = _coverage(results)

    if not synthesize:
        return {"target": value, "type": ttype, "tools_run": list(results.keys()),
                "coverage": coverage, "engine": None, "brief": "", "results": results}

    # Synthesise: Claude (API → subscription bridge) → Ollama fallback.
    digest = _digest(value, ttype, results, coverage)
    brief, engine = "", None
    async with httpx.AsyncClient(timeout=200) as client:
        try:
            brief, engine = await claude_complete(
                system=_SYNTH_SYSTEM, user=digest, http=client, max_tokens=2000,
            )
        except NoClaudeError:
            try:
                r = await client.post(
                    f"{_OLLAMA_URL}/api/generate",
                    json={"model": _OLLAMA_MODEL,
                          "system": _SYNTH_SYSTEM, "prompt": digest, "stream": False},
                )
                r.raise_for_status()
                brief, engine = r.json().get("response", "").strip(), "ollama"
            except Exception as exc:
                brief, engine = f"_Synthesis failed (Ollama): {exc}_", None
        except Exception as exc:
            brief, engine = f"_Synthesis failed: {exc}_", None

    return {
        "target":    value,
        "type":      ttype,
        "tools_run": list(results.keys()),
        "coverage":  coverage,
        "engine":    engine,
        "brief":     brief,
        "results":   results,
    }


# ── Recursive deep investigation (auto-pivot + graph expansion) ─────────────

_DEEP_SYSTEM = """\
You are a senior OSINT analyst. A recursive investigation auto-pivoted from a
seed target across several hops, expanding a knowledge graph. From the per-node
findings, write a concise brief:

## Overview
What the expanded picture shows; the seed and how far it reached.

## Key entities discovered
The most important entities surfaced beyond the seed, and why they matter.

## Risk & red flags
Sanctions/breaches/adverse/threat-intel across the whole expansion. Severity-tag.

## Strongest leads
3-5 specific next moves.

Cite tools/entities. Be specific and terse."""


def _pivots(results: dict, max_branch: int) -> list[tuple[str, str]]:
    """Extract high-signal (value, type) pivots from one investigation's results."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(val, typ):
        v = (val or "").strip()
        k = v.lower()
        if v and k not in seen and "redact" not in k:
            seen.add(k); out.append((v, typ))

    for tool, res in (results or {}).items():
        if not isinstance(res, dict) or res.get("error"):
            continue
        t = tool.lower()
        if "hunter" in t:
            for em in (res.get("emails") or [])[:max_branch]:
                add(em if isinstance(em, str) else em.get("value", ""), "email")
        if "rdap" in t or "whois" in t:
            add(res.get("registrant_email", ""), "email")
            if res.get("registrant_org"):
                add(res["registrant_org"], "company")
        if "etherscan" in t:
            for tx in (res.get("transactions") or [])[:max_branch]:
                add(tx.get("counterparty", ""), "crypto_eth")
        if "companies house" in t:
            for o in (res.get("officers") or [])[:max_branch]:
                add(o.get("name", ""), "name")
            for b in (res.get("beneficial_owners") or [])[:max_branch]:
                add(b.get("name", ""), "name")
    return out[:max_branch]


def _node_summary(value: str, ttype: str, results: dict) -> str:
    """One compact line of the most notable findings for a node."""
    bits = []
    for tool, res in (results or {}).items():
        if not isinstance(res, dict) or res.get("error"):
            continue
        t = tool.lower()
        if ("sanction" in t or "ofac" in t) and res.get("found"):
            bits.append(f"SANCTIONS:{res.get('total', '?')}")
        if "hibp" in t and res.get("breaches"):
            bits.append(f"breaches:{len(res['breaches'])}")
        if "court" in t and res.get("found"):
            bits.append(f"court:{res.get('total', '?')}")
        if "otx" in t and res.get("malicious"):
            bits.append(f"threat:{res.get('pulse_count', 0)}pulses")
        if "cert" in t and res.get("subdomains"):
            bits.append(f"subdomains:{len(res['subdomains'])}")
    return f"[{ttype}] {value} — " + (", ".join(bits) if bits else "no major flags")


async def deep_investigate(seed: str, ttype: str = "auto", graph_db=None,
                           max_hops: int = 1, max_branch: int = 4,
                           global_cap: int = 8) -> dict:
    """
    BFS auto-pivot from a seed: each node is fan-out + persisted (no per-node
    brief); high-signal entities become the next hop. One brief is synthesised
    over the whole expansion. Bounded by max_hops / max_branch / global_cap.
    """
    from graph_intel import persist_investigation as _persist

    seed = (seed or "").strip()
    if not seed:
        return {"error": "empty seed"}
    if ttype in (None, "", "auto"):
        ttype = detect_type(seed)

    visited: set[str] = set()
    summaries: list[str] = []
    investigated: list[dict] = []
    frontier: list[tuple[str, str, int]] = [(seed, ttype, 0)]
    seed_target_id = None

    while frontier and len(visited) < global_cap:
        value, vtype, hop = frontier.pop(0)
        key = value.lower()
        if key in visited:
            continue
        visited.add(key)

        res = await investigate(value, vtype, graph_db=graph_db, synthesize=False)
        if res.get("error"):
            continue
        try:
            g = await _persist(graph_db, res, case_id=None)
            if hop == 0:
                seed_target_id = g.get("target_id")
        except Exception as exc:
            log.debug("deep persist failed: %s", exc)

        summaries.append(("  " * hop) + _node_summary(value, res.get("type", vtype), res.get("results", {})))
        investigated.append({"target": value, "type": res.get("type", vtype), "hop": hop})

        if hop < max_hops:
            for pv, pt in _pivots(res.get("results", {}), max_branch):
                if pv.lower() not in visited and len(visited) + len(frontier) < global_cap:
                    frontier.append((pv, pt, hop + 1))

    # One synthesis over the whole expansion
    digest = (f"SEED: {seed} ({ttype})\nHOPS: {max_hops}\n"
              f"NODES INVESTIGATED ({len(investigated)}):\n" + "\n".join(summaries))
    brief, engine = "", None
    async with httpx.AsyncClient(timeout=200) as client:
        try:
            brief, engine = await claude_complete(
                system=_DEEP_SYSTEM, user=digest, http=client, max_tokens=2000,
            )
        except NoClaudeError:
            try:
                r = await client.post(
                    f"{_OLLAMA_URL}/api/generate",
                    json={"model": _OLLAMA_MODEL, "system": _DEEP_SYSTEM,
                          "prompt": digest, "stream": False},
                )
                r.raise_for_status()
                brief, engine = r.json().get("response", "").strip(), "ollama"
            except Exception as exc:
                brief, engine = f"_Synthesis failed: {exc}_", None
        except Exception as exc:
            brief, engine = f"_Synthesis failed: {exc}_", None

    return {
        "seed": seed, "type": ttype, "max_hops": max_hops,
        "investigated": investigated, "node_count": len(investigated),
        "seed_target_id": seed_target_id, "engine": engine, "brief": brief,
    }

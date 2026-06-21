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

log = logging.getLogger("fieldwork.orchestrator")

_OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://ollama:11434")
_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

_ETH_RE   = re.compile(r"^0x[a-fA-F0-9]{40}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,}$")


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
        ]
    elif ttype == "name":
        t += [
            ("OFAC/Sanctions", check_sanctions(v)),
            ("Court Records",  search_court_records(v)),
            ("Wikidata",       lookup_wikidata(v)),
            ("Adverse Media",  search_adverse_media(v, _OLLAMA_URL, _OLLAMA_MODEL)),
            ("Reddit (search)", reddit_search(v)),
        ]
    elif ttype == "company":
        t += [
            ("Companies House", search_companies_house(v)),
            ("OFAC/Sanctions",  check_sanctions(v)),
            ("Court Records",   search_court_records(v)),
            ("Wikidata",        lookup_wikidata(v)),
            ("Adverse Media",   search_adverse_media(v, _OLLAMA_URL, _OLLAMA_MODEL)),
        ]
    elif ttype == "username":
        t += [
            ("Reddit (user)",   reddit_user(v)),
            ("Reddit (search)", reddit_search(v)),
            ("Wikidata",        lookup_wikidata(v)),
        ]
    elif ttype == "crypto_eth":
        t += [
            ("Etherscan", trace_eth_address(v)),
            ("OTX",       enrich_otx(v)),
        ]
    return t


def _digest(value: str, ttype: str, results: dict) -> str:
    """Compact the raw tool output into LLM context."""
    parts = [f"TARGET: {value}", f"TYPE: {ttype}", "", "TOOL OUTPUT:"]
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


async def investigate(value: str, ttype: str = "auto", graph_db=None) -> dict:
    """Run the full orchestrated investigation and return the brief + raw data."""
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

    # Synthesise: Claude (API → subscription bridge) → Ollama fallback.
    digest = _digest(value, ttype, results)
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
        "engine":    engine,
        "brief":     brief,
        "results":   results,
    }

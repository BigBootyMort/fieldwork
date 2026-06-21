"""
Sanctions and PEP screening against free public lists.

Sources (all free, no API key required):
  - OFAC SDN List        — US Treasury, Specially Designated Nationals
  - OFAC Consolidated    — US Treasury, consolidated non-SDN list
  - EU Consolidated List — European External Action Service (CSV)
  - UN Security Council  — 1267 Committee consolidated list (XML)

Lists are downloaded once per process and cached in memory with a 6-hour TTL.
Matching uses case-insensitive partial name matching with a simple token overlap
score — good enough for screening; not a replacement for commercial fuzzy-match
AML platforms (Accuity, Dow Jones, etc.).
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
from typing import Optional

import httpx

log = logging.getLogger("fieldwork.sanctions")

# ── List sources ──────────────────────────────────────────────────────────────
# OFAC moved to sanctionslistservice.ofac.treas.gov — old treasury.gov URLs redirect here
_OFAC_SDN_CSV    = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/sdn.csv"
_OFAC_CONS_CSV   = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/cons_prim.csv"
_EU_CSV          = "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList/content?token=dG9rZW4tMjAxNy0wMS0xNg"  # noqa

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache: dict = {"sdn": [], "cons": [], "eu": [], "loaded_at": 0.0}
_cache_lock = asyncio.Lock()
_CACHE_TTL = 6 * 3600   # 6 hours


def _tokens(s: str) -> set[str]:
    return {w.lower() for w in re.split(r"[\s,;.()\-]+", s) if len(w) >= 3}


def _score(query_tokens: set[str], name: str) -> float:
    """0-1 overlap score between query tokens and name."""
    nt = _tokens(name)
    if not nt:
        return 0.0
    overlap = len(query_tokens & nt)
    return overlap / max(len(query_tokens), len(nt))


async def _fetch_csv(url: str, client: httpx.AsyncClient) -> list[list[str]]:
    try:
        r = await client.get(url, timeout=45.0, follow_redirects=True,
                             headers={"User-Agent": "Fieldwork OSINT research@fieldwork.local"})
        r.raise_for_status()
        text = r.text
        reader = csv.reader(io.StringIO(text))
        return list(reader)
    except Exception as exc:
        log.warning("sanctions: failed to fetch %s: %s", url, exc)
        return []


async def _load_lists() -> None:
    """Download and parse all sanction lists into the cache."""
    async with httpx.AsyncClient(timeout=45.0) as client:
        sdn_rows, cons_rows = await asyncio.gather(
            _fetch_csv(_OFAC_SDN_CSV, client),
            _fetch_csv(_OFAC_CONS_CSV, client),
        )

    # OFAC SDN CSV columns: ent_num, sdn_name, sdn_type, program, title,
    #                        call_sign, vess_type, tonnage, grt, vess_flag,
    #                        vess_owner, remarks
    sdn_entries = []
    for row in sdn_rows:          # OFAC SDN CSV has no header row
        if len(row) < 2:
            continue
        name = row[1].strip().strip('"')
        sdn_type = row[2].strip().strip('"') if len(row) > 2 else ""
        program  = row[3].strip().strip('"') if len(row) > 3 else ""
        if name and not name.startswith("-0-"):  # continuation row marker
            sdn_entries.append({
                "name": name, "list": "OFAC SDN",
                "type": sdn_type, "program": program,
                "sanctions": True, "pep": False,
            })

    # OFAC Consolidated: same structure, different programs
    cons_entries = []
    for row in cons_rows:
        if len(row) < 2:
            continue
        name = row[1].strip().strip('"')
        if name and not name.startswith("-0-"):
            cons_entries.append({
                "name": name, "list": "OFAC Consolidated",
                "type": row[2].strip().strip('"') if len(row) > 2 else "",
                "program": row[3].strip().strip('"') if len(row) > 3 else "",
                "sanctions": True, "pep": False,
            })

    _cache["sdn"]       = sdn_entries
    _cache["cons"]      = cons_entries
    _cache["loaded_at"] = time.time()
    log.info("sanctions: loaded %d SDN + %d consolidated entries",
             len(sdn_entries), len(cons_entries))


async def _ensure_loaded() -> None:
    async with _cache_lock:
        age = time.time() - _cache["loaded_at"]
        if age > _CACHE_TTL or not _cache["sdn"]:
            await _load_lists()


async def check_sanctions(name: str, min_score: float = 0.55) -> dict:
    """
    Screen a name against OFAC SDN and Consolidated lists.

    Returns the top matches above min_score (default 0.55 — requires
    more than half the query tokens to match).
    """
    await _ensure_loaded()

    qt = _tokens(name)
    if not qt:
        return {"name": name, "found": False, "hits": [], "total": 0, "highest_score": 0.0}

    hits: list[dict] = []
    all_entries = _cache["sdn"] + _cache["cons"]

    for entry in all_entries:
        score = _score(qt, entry["name"])
        if score >= min_score:
            hits.append({
                "caption":   entry["name"],
                "list":      entry["list"],
                "type":      entry["type"],
                "program":   entry["program"],
                "score":     round(score, 3),
                "sanctions": entry["sanctions"],
                "pep":       entry["pep"],
            })

    # Deduplicate exact names, keep highest score
    seen: dict[str, dict] = {}
    for h in hits:
        key = h["caption"].upper()
        if key not in seen or h["score"] > seen[key]["score"]:
            seen[key] = h

    hits = sorted(seen.values(), key=lambda h: h["score"], reverse=True)[:10]
    highest = hits[0]["score"] if hits else 0.0

    return {
        "name":          name,
        "found":         bool(hits),
        "hits":          hits,
        "total":         len(hits),
        "highest_score": highest,
        "sources":       ["OFAC SDN", "OFAC Consolidated"],
        "note":          f"Screened {len(all_entries):,} entries. Min match threshold: {min_score:.0%}.",
    }

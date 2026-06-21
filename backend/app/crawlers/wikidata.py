"""Wikidata + Wikipedia entity lookup (free, no key).

Structured biographical / organisational facts ideal for auto-populating
research briefs: official description, key properties (occupation, country,
date of birth, etc.), and the Wikipedia summary extract.
"""
import logging

import httpx

log = logging.getLogger("fieldwork.wikidata")

_WD_API = "https://www.wikidata.org/w/api.php"
_WP_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_HEADERS = {"User-Agent": "Fieldwork-OSINT/1.0 (research)", "Accept": "application/json"}

# Common, high-signal property IDs → human label
_PROPS = {
    "P31":  "instance of",
    "P106": "occupation",
    "P27":  "citizenship",
    "P569": "date of birth",
    "P570": "date of death",
    "P19":  "place of birth",
    "P159": "headquarters",
    "P571": "inception",
    "P112": "founded by",
    "P169": "CEO",
    "P1454": "legal form",
    "P452": "industry",
}


async def lookup_wikidata(query: str, limit: int = 5) -> dict:
    """Search Wikidata for an entity and return structured facts for the top hit."""
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            sr = await client.get(_WD_API, params={
                "action": "wbsearchentities", "search": query,
                "language": "en", "format": "json", "limit": limit,
            })
            sr.raise_for_status()
            hits = sr.json().get("search", [])
            if not hits:
                return {"query": query, "found": False, "reason": "No Wikidata match"}

            candidates = [{
                "id":          h.get("id", ""),
                "label":       h.get("label", ""),
                "description": h.get("description", ""),
                "url":         h.get("concepturi", "") or f"https://www.wikidata.org/wiki/{h.get('id','')}",
            } for h in hits]

            top_id = candidates[0]["id"]
            ent = await client.get(_WD_API, params={
                "action": "wbgetentities", "ids": top_id, "format": "json",
                "props": "labels|descriptions|claims|sitelinks",
                "languages": "en",
            })
            ent.raise_for_status()
            edata = ent.json().get("entities", {}).get(top_id, {}) or {}
    except Exception as exc:
        log.warning("Wikidata lookup failed for %r: %s", query, exc)
        return {"query": query, "found": False, "error": str(exc)}

    # Extract facts from claims; collect entity-reference Q-ids to resolve to labels
    facts = []
    qids: set = set()
    claims = edata.get("claims", {}) or {}
    for pid, label in _PROPS.items():
        if pid not in claims:
            continue
        for c in claims[pid][:3]:
            val = _render_value(c)
            if not val:
                continue
            if isinstance(val, str) and val.startswith("Q") and val[1:].isdigit():
                qids.add(val)
            facts.append({"property": label, "value": val})

    # Batch-resolve Q-ids → English labels for readability
    if qids:
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
                lr = await client.get(_WD_API, params={
                    "action": "wbgetentities", "ids": "|".join(list(qids)[:50]),
                    "format": "json", "props": "labels", "languages": "en",
                })
                if lr.status_code == 200:
                    ents = lr.json().get("entities", {}) or {}
                    labels = {qid: (e.get("labels", {}).get("en", {}) or {}).get("value", qid)
                              for qid, e in ents.items()}
                    for f in facts:
                        if f["value"] in labels:
                            f["value"] = labels[f["value"]]
        except Exception as e:
            log.debug("label resolution failed: %s", e)

    # Wikipedia extract
    wp_title = (edata.get("sitelinks", {}) or {}).get("enwiki", {}).get("title", "")
    wp_extract, wp_url = "", ""
    if wp_title:
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
                wp = await client.get(_WP_SUMMARY.format(title=wp_title.replace(" ", "_")))
                if wp.status_code == 200:
                    j = wp.json()
                    wp_extract = j.get("extract", "")
                    wp_url = (j.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
        except Exception as e:
            log.debug("wikipedia summary failed: %s", e)

    return {
        "query":       query,
        "found":       True,
        "id":          top_id,
        "label":       candidates[0]["label"],
        "description": candidates[0]["description"],
        "wikidata_url": f"https://www.wikidata.org/wiki/{top_id}",
        "facts":       facts,
        "wikipedia_extract": wp_extract,
        "wikipedia_url":     wp_url,
        "other_matches": candidates[1:],
    }


def _render_value(claim: dict) -> str:
    """Best-effort rendering of a Wikidata claim's main value."""
    try:
        dv = claim["mainsnak"]["datavalue"]["value"]
    except (KeyError, TypeError):
        return ""
    if isinstance(dv, dict):
        if "id" in dv:        # entity reference (Qxxxx) — return the id; label resolution is out of scope
            return dv["id"]
        if "time" in dv:      # date
            return dv["time"].lstrip("+")[:10]
        if "amount" in dv:
            return dv["amount"]
        if "text" in dv:
            return dv["text"]
    return str(dv)

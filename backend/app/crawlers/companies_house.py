"""UK Companies House — official company registry (free API key).

Far richer than OpenCorporates for UK entities: gives beneficial ownership
(Persons with Significant Control / PSC), full officer lists, and filing
history. Critical for "who really owns this company" due-diligence questions.

Get a free key at https://developer.company-information.service.gov.uk/
Set COMPANIES_HOUSE_KEY. Auth is HTTP Basic with the key as username and an
empty password.
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.companies_house")

_BASE = "https://api.company-information.service.gov.uk"
_HEADERS = {"Accept": "application/json", "User-Agent": "Fieldwork OSINT"}


def _auth():
    key = os.getenv("COMPANIES_HOUSE_KEY", "")
    return (key, "") if key else None


async def search_companies_house(query: str, limit: int = 10) -> dict:
    """Search the UK register by company name. Returns top matches."""
    auth = _auth()
    if not auth:
        return {"query": query, "found": False, "reason": "COMPANIES_HOUSE_KEY not configured"}
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, auth=auth) as client:
            r = await client.get(f"{_BASE}/search/companies",
                                  params={"q": query, "items_per_page": limit})
            if r.status_code == 401:
                return {"query": query, "found": False, "reason": "Invalid COMPANIES_HOUSE_KEY"}
            if r.status_code == 429:
                return {"query": query, "found": False, "reason": "Rate limited"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("Companies House search failed for %r: %s", query, exc)
        return {"query": query, "found": False, "error": str(exc)}

    items = []
    for it in data.get("items", [])[:limit]:
        addr = it.get("address", {}) or {}
        items.append({
            "company_name":   it.get("title", ""),
            "company_number": it.get("company_number", ""),
            "status":         it.get("company_status", ""),
            "type":           it.get("company_type", ""),
            "incorporated":   it.get("date_of_creation", ""),
            "address":        it.get("address_snippet", "")
                              or ", ".join(filter(None, [addr.get("locality"), addr.get("postal_code")])),
            "url":            f"https://find-and-update.company-information.service.gov.uk/company/{it.get('company_number','')}",
        })
    return {"query": query, "found": bool(items), "companies": items, "total": data.get("total_results", len(items))}


async def companies_house_company(number: str) -> dict:
    """Full profile for a company number: profile + officers + PSC (beneficial owners)."""
    auth = _auth()
    if not auth:
        return {"number": number, "found": False, "reason": "COMPANIES_HOUSE_KEY not configured"}
    out: dict = {"number": number, "found": False}
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, auth=auth) as client:
            prof = await client.get(f"{_BASE}/company/{number}")
            if prof.status_code == 404:
                return {"number": number, "found": False, "reason": "Company not found"}
            prof.raise_for_status()
            p = prof.json()
            reg = p.get("registered_office_address", {}) or {}
            out.update({
                "found":        True,
                "company_name": p.get("company_name", ""),
                "status":       p.get("company_status", ""),
                "type":         p.get("type", ""),
                "incorporated": p.get("date_of_creation", ""),
                "sic_codes":    p.get("sic_codes", []),
                "address":      ", ".join(filter(None, [
                                    reg.get("address_line_1"), reg.get("locality"),
                                    reg.get("postal_code"), reg.get("country")])),
            })
            # Officers
            try:
                off = await client.get(f"{_BASE}/company/{number}/officers", params={"items_per_page": 35})
                if off.status_code == 200:
                    out["officers"] = [{
                        "name":           o.get("name", ""),
                        "role":           o.get("officer_role", ""),
                        "appointed":      o.get("appointed_on", ""),
                        "resigned":       o.get("resigned_on", ""),
                        "nationality":    o.get("nationality", ""),
                        "occupation":     o.get("occupation", ""),
                    } for o in off.json().get("items", [])]
            except Exception as e:
                log.debug("officers fetch failed: %s", e)
            # Persons with Significant Control (beneficial ownership)
            try:
                psc = await client.get(f"{_BASE}/company/{number}/persons-with-significant-control",
                                       params={"items_per_page": 35})
                if psc.status_code == 200:
                    out["beneficial_owners"] = [{
                        "name":         s.get("name", ""),
                        "kind":         s.get("kind", ""),
                        "nationality":  s.get("nationality", ""),
                        "notified_on":  s.get("notified_on", ""),
                        "nature":       s.get("natures_of_control", []),
                    } for s in psc.json().get("items", [])]
            except Exception as e:
                log.debug("psc fetch failed: %s", e)
    except Exception as exc:
        log.warning("Companies House company fetch failed for %r: %s", number, exc)
        return {"number": number, "found": False, "error": str(exc)}
    return out

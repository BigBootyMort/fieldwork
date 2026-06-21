"""
Hunter.io domain email discovery.
https://hunter.io/api-reference

Requires a HUNTER_API_KEY (free plan available).
Returns up to 20 email addresses for a given domain.
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.hunter")

_API_KEY = os.getenv("HUNTER_API_KEY", "")
_URL     = "https://api.hunter.io/v2/domain-search"


async def hunt_domain_emails(domain: str) -> dict:
    """
    Find email addresses associated with a domain via Hunter.io.
    Returns emails with type, confidence, name, and position.
    """
    if not _API_KEY:
        return {"domain": domain, "found": False, "reason": "no key — set HUNTER_API_KEY"}

    params = {"domain": domain, "api_key": _API_KEY, "limit": 20}

    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Fieldwork OSINT"}) as client:
            r = await client.get(_URL, params=params)
            if r.status_code == 401:
                return {"domain": domain, "found": False, "reason": "Invalid Hunter API key"}
            if r.status_code == 429:
                return {"domain": domain, "found": False, "reason": "Hunter rate limit reached"}
            r.raise_for_status()
            data = r.json().get("data", {})
    except Exception as exc:
        log.warning("Hunter lookup failed for %s: %s", domain, exc)
        return {"domain": domain, "found": False, "reason": str(exc)}

    raw_emails = data.get("emails", [])
    emails = [
        {
            "value":      e.get("value", ""),
            "type":       e.get("type", ""),
            "confidence": e.get("confidence", 0),
            "first_name": e.get("first_name", ""),
            "last_name":  e.get("last_name", ""),
            "position":   e.get("position", ""),
        }
        for e in raw_emails
    ]

    return {
        "domain":       domain,
        "found":        bool(emails),
        "emails":       emails,
        "organization": data.get("organization", ""),
        "pattern":      data.get("pattern", ""),
    }

"""
EmailRep.io email reputation check.
https://emailrep.io/docs

Free tier works without a key (rate limited).
Set EMAILREP_KEY for higher quotas.
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.emailrep")

_KEY = os.getenv("EMAILREP_KEY", "")
_URL = "https://emailrep.io/{email}"


async def check_email_rep(email: str) -> dict:
    """
    Check the reputation of an email address via EmailRep.io.
    Returns reputation score, suspicious flag, and details.
    """
    headers: dict = {
        "Accept":     "application/json",
        "User-Agent": "Fieldwork OSINT",
    }
    if _KEY:
        headers["Key"] = _KEY

    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            r = await client.get(_URL.format(email=email))
            if r.status_code == 400:
                return {"email": email, "found": False, "reason": "Invalid email address"}
            if r.status_code == 429:
                return {"email": email, "found": False, "reason": "Rate limited — set EMAILREP_KEY for higher limits"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("EmailRep lookup failed for %s: %s", email, exc)
        return {"email": email, "found": False, "reason": str(exc)}

    return {
        "email":      email,
        "found":      True,
        "reputation": data.get("reputation", "unknown"),
        "suspicious": bool(data.get("suspicious", False)),
        "references": data.get("references", 0),
        "details":    data.get("details", {}),
    }

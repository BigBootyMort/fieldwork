"""WHOIS history via ViewDNS.info."""
import logging
import os
import httpx

log = logging.getLogger("fieldwork.whois_history")
VIEWDNS_KEY = os.getenv("VIEWDNS_KEY", "")
_VIEWDNS_URL = "https://api.viewdns.info/whoishistory/"


async def get_whois_history(domain: str) -> dict:
    """Fetch WHOIS registration history for a domain via ViewDNS.info."""
    key = os.getenv("VIEWDNS_KEY", VIEWDNS_KEY)
    if not key:
        return {"domain": domain, "found": False, "reason": "no key — set VIEWDNS_KEY"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _VIEWDNS_URL,
                params={"domain": domain, "apikey": key, "output": "json"},
            )

        if resp.status_code != 200:
            return {"domain": domain, "found": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        raw_records = (data.get("response") or {}).get("result", []) or []
        records = [
            {
                "date": r.get("date", ""),
                "registrant_name": r.get("registrant_name", ""),
                "registrant_email": r.get("registrant_email", ""),
                "registrant_org": r.get("registrant_org", ""),
                "expiry_date": r.get("expiry_date", ""),
            }
            for r in raw_records
        ]

        return {
            "domain": domain,
            "found": bool(records),
            "records": records,
            "total": len(records),
        }
    except Exception as exc:
        log.warning("ViewDNS WHOIS history error for %r: %s", domain, exc)
        return {"domain": domain, "found": False, "error": str(exc)}

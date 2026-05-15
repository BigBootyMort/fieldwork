"""
Dehashed — credential breach search across 15 billion+ leaked records.

Searches by email, username, IP, name, address, phone, or VIN.
Requires: DEHASHED_EMAIL and DEHASHED_KEY in .env
Sign up:  https://www.dehashed.com/register  (paid, ~$5/month)
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.dehashed")

_BASE    = "https://api.dehashed.com/search"
_TIMEOUT = 15.0


def _creds() -> tuple[str, str] | None:
    email = os.getenv("DEHASHED_EMAIL", "")
    key   = os.getenv("DEHASHED_KEY",   "")
    if email and key:
        return (email, key)
    return None


async def search(
    query: str,
    field: str = "auto",
    size: int = 20,
) -> dict:
    """
    Search Dehashed breach database.

    field: one of "auto", "email", "username", "ip_address",
                  "name", "address", "phone", "vin"
           "auto" uses the generic query param without field restriction.
    """
    creds = _creds()
    if not creds:
        return {"error": "DEHASHED_EMAIL / DEHASHED_KEY not set", "entries": []}

    # Build query string — Dehashed supports field:value syntax
    if field != "auto" and field:
        q = f'{field}:"{query}"'
    else:
        q = query

    params = {"query": q, "size": min(size, 100)}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _BASE,
                params=params,
                auth=creds,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        log.warning("Dehashed HTTP %s for query %r", exc.response.status_code, query)
        return {"error": str(exc), "entries": []}
    except Exception as exc:
        log.warning("Dehashed error: %s", exc)
        return {"error": str(exc), "entries": []}

    raw_entries = data.get("entries") or []
    total       = data.get("total", len(raw_entries))
    balance     = data.get("balance")

    entries = []
    for e in raw_entries:
        # Never return plain-text passwords — only indicate whether one was present
        has_password = bool(e.get("password") or e.get("hashed_password"))
        entries.append({
            "id":           e.get("id"),
            "email":        e.get("email"),
            "username":     e.get("username"),
            "name":         e.get("name"),
            "ip_address":   e.get("ip_address"),
            "phone":        e.get("phone"),
            "address":      e.get("address"),
            "database":     e.get("database_name"),
            "has_password": has_password,
        })

    return {
        "entries": entries,
        "total":   total,
        "balance": balance,
        "query":   query,
    }

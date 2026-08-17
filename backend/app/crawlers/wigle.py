"""
WiGLE Wi-Fi geolocation — https://api.wigle.net/

Given a Wi-Fi network (BSSID/MAC or SSID name), return where WiGLE's crowd-sourced
wardriving database has observed it — trilaterated lat/long + address. Useful for placing
a device/access point geographically from its MAC or network name.

Auth: WiGLE uses HTTP Basic auth with an API Name + API Token from your account page
(https://wigle.net/account). That page also shows an "Encoded for use" value = the
base64 of "name:token" — store THAT single string as WIGLE_API_KEY and we send it
verbatim as `Authorization: Basic <key>`. (One secret, matches the app's one-key-per-source
convention.) Free tier has a modest daily query quota.
"""
import logging
import os
import re

import httpx

log = logging.getLogger("fieldwork.wigle")

_URL = "https://api.wigle.net/api/v2/network/search"
# BSSID = the MAC of an access point, e.g. AA:BB:CC:DD:EE:FF (colon/dash/dot tolerated below).
_MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)


def _norm_mac(q: str) -> str | None:
    """Return a colon-form MAC if q looks like one, else None."""
    cleaned = q.strip().replace("-", ":").lower()
    return cleaned if _MAC_RE.match(cleaned) else None


async def enrich_wifi_wigle(query: str) -> dict:
    """Look up a Wi-Fi network by BSSID (MAC) or SSID (name) via WiGLE."""
    query = (query or "").strip()
    if not query:
        return {"query": query, "found": False, "reason": "Empty query"}

    key = os.getenv("WIGLE_API_KEY", "").strip()
    if not key:
        return {"query": query, "found": False,
                "reason": "WIGLE_API_KEY not set — add it via the API Key Manager (🔑)"}

    mac = _norm_mac(query)
    if mac:
        params = {"netid": mac, "resultsPerPage": 10}
        search_type = "bssid"
    else:
        # Exact SSID match keeps results focused; WiGLE also offers ssidlike for fuzzy.
        params = {"ssid": query, "resultsPerPage": 10}
        search_type = "ssid"

    headers = {"Accept": "application/json", "User-Agent": "Fieldwork OSINT",
               "Authorization": f"Basic {key}"}

    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            r = await client.get(_URL, params=params)
            if r.status_code == 401:
                return {"query": query, "found": False,
                        "reason": "Invalid WIGLE_API_KEY (use the 'Encoded for use' token)"}
            if r.status_code == 429:
                return {"query": query, "found": False,
                        "reason": "WiGLE daily quota exceeded — try again tomorrow"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("WiGLE lookup failed for %r: %s", query, exc)
        return {"query": query, "found": False, "reason": str(exc)}

    if not data.get("success", False):
        # WiGLE returns success:false with a message on quota/param errors.
        return {"query": query, "found": False,
                "reason": data.get("message", "WiGLE returned no success")}

    raw = data.get("results") or []
    results = []
    for n in raw[:10]:
        if not isinstance(n, dict):
            continue
        results.append({
            "ssid":       n.get("ssid") or "",
            "bssid":      n.get("netid") or "",
            "lat":        n.get("trilat"),
            "lon":        n.get("trilong"),
            "encryption": n.get("encryption") or "",
            "country":    n.get("country") or "",
            "region":     n.get("region") or "",
            "city":       n.get("city") or "",
            "last_seen":  n.get("lastupdt") or "",
        })

    if not results:
        return {"query": query, "found": False, "search_type": search_type,
                "reason": "No observations in WiGLE for this network"}

    return {
        "query":         query,
        "found":         True,
        "search_type":   search_type,
        "total_results": data.get("totalResults", len(results)),
        "results":       results,
    }

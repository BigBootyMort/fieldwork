"""
Censys — internet-wide scan data (hosts, certificates, open ports).

Free tier: 250 queries / month.
Requires: CENSYS_API_ID and CENSYS_API_SECRET in .env
Sign up:  https://censys.io/register
"""
import logging
import os

import httpx

log = logging.getLogger("fieldwork.censys")

_BASE    = "https://search.censys.io/api/v2"
_TIMEOUT = 15.0


def _creds() -> tuple[str, str] | None:
    api_id     = os.getenv("CENSYS_API_ID", "")
    api_secret = os.getenv("CENSYS_API_SECRET", "")
    if api_id and api_secret:
        return (api_id, api_secret)
    return None


async def search_hosts(query: str, per_page: int = 25) -> dict:
    """
    Search Censys hosts index.
    query can be a plain-text search or Censys query syntax, e.g.:
      "services.port: 22 AND location.country: DE"
      "autonomous_system.name: Google"
    """
    creds = _creds()
    if not creds:
        return {"error": "CENSYS_API_ID / CENSYS_API_SECRET not set", "hosts": []}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/hosts/search",
                params={"q": query, "per_page": min(per_page, 100)},
                auth=creds,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        log.warning("Censys HTTP %s for query %r", exc.response.status_code, query)
        return {"error": str(exc), "hosts": []}
    except Exception as exc:
        log.warning("Censys error: %s", exc)
        return {"error": str(exc), "hosts": []}

    hits  = data.get("result", {}).get("hits", [])
    total = data.get("result", {}).get("total", 0)

    hosts = []
    for h in hits:
        services = h.get("services", [])
        ports    = sorted({s.get("port") for s in services if s.get("port")})
        protos   = list({s.get("transport_protocol", "") for s in services if s.get("transport_protocol")})
        asn      = h.get("autonomous_system", {})

        hosts.append({
            "ip":           h.get("ip"),
            "asn":          asn.get("asn"),
            "asn_name":     asn.get("name"),
            "country_code": h.get("location", {}).get("country_code"),
            "country":      h.get("location", {}).get("country"),
            "city":         h.get("location", {}).get("city"),
            "lat":          h.get("location", {}).get("coordinates", {}).get("latitude"),
            "lng":          h.get("location", {}).get("coordinates", {}).get("longitude"),
            "ports":        ports,
            "protocols":    protos,
            "services":     [
                {
                    "port":            s.get("port"),
                    "name":            s.get("service_name"),
                    "transport":       s.get("transport_protocol"),
                    "banner":          (s.get("banner") or "")[:300],
                }
                for s in services[:10]
            ],
        })

    return {"hosts": hosts, "total": total, "query": query}


async def get_host(ip: str) -> dict:
    """Fetch full details for a single IP address."""
    creds = _creds()
    if not creds:
        return {"error": "CENSYS_API_ID / CENSYS_API_SECRET not set"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/hosts/{ip}",
                auth=creds,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json().get("result", {})
    except Exception as exc:
        log.warning("Censys host lookup failed for %s: %s", ip, exc)
        return {"error": str(exc)}

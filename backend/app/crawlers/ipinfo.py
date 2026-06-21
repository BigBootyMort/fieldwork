"""
IPinfo.io IP enrichment.
https://ipinfo.io/{ip}/json

Works without a token at lower rate limits.
Set IPINFO_TOKEN for higher quotas.

Skips private/loopback IPs gracefully.
"""
import ipaddress
import logging
import os

import httpx

log = logging.getLogger("fieldwork.ipinfo")

_TOKEN = os.getenv("IPINFO_TOKEN", "")
_URL   = "https://ipinfo.io/{ip}/json"

_PRIVATE_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        return False


async def enrich_ip_ipinfo(ip: str) -> dict:
    """
    Enrich an IP address with IPinfo.io data.
    Returns location, ASN, org, hostname, and timezone.
    """
    if _is_private(ip):
        return {"ip": ip, "found": False, "reason": "Private/loopback IP — skipped"}

    headers: dict = {"Accept": "application/json", "User-Agent": "Fieldwork OSINT"}
    if _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            r = await client.get(_URL.format(ip=ip))
            if r.status_code == 404:
                return {"ip": ip, "found": False, "reason": "Not found"}
            if r.status_code == 429:
                return {"ip": ip, "found": False, "reason": "Rate limited — set IPINFO_TOKEN for higher limits"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("IPinfo lookup failed for %s: %s", ip, exc)
        return {"ip": ip, "found": False, "reason": str(exc)}

    if "error" in data:
        return {"ip": ip, "found": False, "reason": data["error"].get("message", "unknown error")}

    org  = data.get("org", "")
    asn  = org.split(" ")[0] if org and org.startswith("AS") else ""

    return {
        "ip":       ip,
        "found":    True,
        "hostname": data.get("hostname", ""),
        "city":     data.get("city", ""),
        "region":   data.get("region", ""),
        "country":  data.get("country", ""),
        "org":      org,
        "asn":      asn,
        "postal":   data.get("postal", ""),
        "timezone": data.get("timezone", ""),
    }

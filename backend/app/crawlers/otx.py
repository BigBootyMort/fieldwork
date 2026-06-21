"""AlienVault OTX — Open Threat Exchange IOC enrichment.

Rich, free threat-intel context for IPs, domains, hostnames, and file hashes:
community "pulses" (threat reports), malware associations, passive DNS, and
related URLs. Complements GreyNoise/VirusTotal with attribution context.

Free; set OTX_API_KEY for higher limits and full pulse access.
Get a key at https://otx.alienvault.com/api
"""
import ipaddress
import logging
import os
import re

import httpx

log = logging.getLogger("fieldwork.otx")

_BASE = "https://otx.alienvault.com/api/v1/indicators"
_HASH_RE = re.compile(r"^[A-Fa-f0-9]{32}$|^[A-Fa-f0-9]{40}$|^[A-Fa-f0-9]{64}$")


def _classify(indicator: str) -> str:
    try:
        ip = ipaddress.ip_address(indicator)
        return "IPv6" if ip.version == 6 else "IPv4"
    except ValueError:
        pass
    if _HASH_RE.match(indicator):
        return "file"
    if "." in indicator:
        return "domain"
    return "domain"


async def enrich_otx(indicator: str) -> dict:
    """Look up an IP/domain/hostname/hash in AlienVault OTX."""
    itype = _classify(indicator)
    headers = {"User-Agent": "Fieldwork OSINT", "Accept": "application/json"}
    key = os.getenv("OTX_API_KEY", "")
    if key:
        headers["X-OTX-API-KEY"] = key

    section = "general"
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            r = await client.get(f"{_BASE}/{itype}/{indicator}/{section}")
            if r.status_code == 404:
                return {"indicator": indicator, "type": itype, "found": False, "reason": "Not found in OTX"}
            if r.status_code in (401, 403):
                return {"indicator": indicator, "type": itype, "found": False,
                        "reason": "OTX auth required — set OTX_API_KEY"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("OTX lookup failed for %r: %s", indicator, exc)
        return {"indicator": indicator, "type": itype, "found": False, "error": str(exc)}

    pulse_info = data.get("pulse_info", {}) or {}
    pulses = pulse_info.get("pulses", []) or []
    out = {
        "indicator":    indicator,
        "type":         itype,
        "found":        True,
        "pulse_count":  pulse_info.get("count", len(pulses)),
        "pulses":       [{
            "name":         p.get("name", ""),
            "author":       (p.get("author") or {}).get("username", "") if isinstance(p.get("author"), dict) else p.get("author_name", ""),
            "created":      p.get("created", ""),
            "tags":         p.get("tags", [])[:8],
            "malware":      [m.get("display_name", m) if isinstance(m, dict) else m
                             for m in (p.get("malware_families") or [])][:5],
            "adversary":    p.get("adversary", ""),
        } for p in pulses[:8]],
        "reputation":   data.get("reputation"),
        "country":      data.get("country_name", ""),
        "asn":          data.get("asn", ""),
        "url":          f"https://otx.alienvault.com/indicator/{itype.lower()}/{indicator}",
    }
    out["malicious"] = out["pulse_count"] > 0
    return out

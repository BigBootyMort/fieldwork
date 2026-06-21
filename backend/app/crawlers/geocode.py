"""Nominatim geocoding (OpenStreetMap, free, no key).

Forward geocode (address → coordinates) and reverse geocode
(coordinates → address) to feed the map view. A descriptive
User-Agent is required by the Nominatim usage policy.
"""
import logging

import httpx

log = logging.getLogger("fieldwork.geocode")

_BASE = "https://nominatim.openstreetmap.org"
_HEADERS = {"User-Agent": "Fieldwork-OSINT/1.0 (research@fieldwork.local)", "Accept": "application/json"}


async def geocode(query: str, limit: int = 5) -> dict:
    """Resolve an address / place name to coordinates."""
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            r = await client.get(f"{_BASE}/search", params={
                "q": query, "format": "json", "addressdetails": 1, "limit": limit,
            })
            if r.status_code == 429:
                return {"query": query, "found": False, "reason": "Rate limited by Nominatim"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("Geocode failed for %r: %s", query, exc)
        return {"query": query, "found": False, "error": str(exc)}

    results = []
    for it in data[:limit]:
        results.append({
            "display_name": it.get("display_name", ""),
            "lat":          float(it.get("lat", 0) or 0),
            "lon":          float(it.get("lon", 0) or 0),
            "type":         it.get("type", ""),
            "category":     it.get("class", ""),
            "importance":   it.get("importance", 0),
            "osm_url":      f"https://www.openstreetmap.org/{it.get('osm_type','')[:1]}{it.get('osm_id','')}"
                            if it.get("osm_id") else "",
        })
    return {"query": query, "found": bool(results), "results": results}


async def reverse_geocode(lat: float, lon: float) -> dict:
    """Resolve coordinates to a nearest address."""
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            r = await client.get(f"{_BASE}/reverse", params={
                "lat": lat, "lon": lon, "format": "json", "addressdetails": 1,
            })
            r.raise_for_status()
            d = r.json()
    except Exception as exc:
        log.warning("Reverse geocode failed for %s,%s: %s", lat, lon, exc)
        return {"lat": lat, "lon": lon, "found": False, "error": str(exc)}

    if "error" in d:
        return {"lat": lat, "lon": lon, "found": False, "reason": d["error"]}
    return {
        "lat":          lat,
        "lon":          lon,
        "found":        True,
        "display_name": d.get("display_name", ""),
        "address":      d.get("address", {}),
    }

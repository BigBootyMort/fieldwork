"""
Phase 12 — Geospatial Map View.

Fetches Location nodes from Neo4j and serves them to the Leaflet.js
frontend.  Also provides on-demand Nominatim geocoding so that Location
nodes which only have an address string can acquire lat/lng coordinates
and move from the "ungeolocated" pile to the map.

Public API
----------
get_locations(graph_db, limit)             → {"locations": [...], "ungeolocated": [...], "total": N}
geocode_location(graph_db, loc_id)         → {"id", "lat", "lng", ...}  |  {"error": "..."}
geolocate_ip(graph_db, ip)                 → {"id", "lat", "lng", "city", "country", ...}
add_manual_location(graph_db, name, ...)   → {"id", "lat", "lng", ...}
"""

import hashlib
import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("fieldwork.map_view")

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_HEADERS   = {
    "User-Agent":      "Fieldwork-OSINT/1.0 (investigative-journalism-tool)",
    "Accept-Language": "en",
}

# Cypher that fetches every Location with its connected-entity summary.
# We COALESCE lat/latitude and lng/longitude so the schema is flexible.
_LOCATION_CYPHER = """
MATCH (l:Location)
OPTIONAL MATCH (l)-[r]-(n)
WHERE n.id IS NOT NULL
WITH l,
     l {.*} AS props,
     collect(DISTINCT {
         id:           n.id,
         label:        labels(n)[0],
         display_name: COALESCE(n.name, n.address, n.handle, n.title, n.id)
     }) AS all_connected
WITH l, props,
     [x IN all_connected WHERE x.id IS NOT NULL] AS connected,
     COALESCE(l.lat, l.latitude)   AS lat,
     COALESCE(l.lng, l.longitude)  AS lng
RETURN
    l.id      AS id,
    l.name    AS name,
    lat, lng,
    l.country AS country,
    l.city    AS city,
    l.address AS address,
    props,
    connected,
    size(connected) AS degree
ORDER BY degree DESC
LIMIT $limit
"""


async def get_locations(graph_db, limit: int = 500) -> dict:
    """
    Return all Location nodes split into two lists:

    - **locations**    : nodes with valid lat/lng — ready to plot.
    - **ungeolocated** : nodes that have address text but no coordinates.
    """
    geolocated:   list[dict] = []
    ungeolocated: list[dict] = []

    async with graph_db.driver.session() as session:
        try:
            result = await session.run(_LOCATION_CYPHER, limit=limit)
            async for r in result:
                connected = [
                    c for c in (r["connected"] or [])
                    if c.get("id") and c.get("id") != r["id"]
                ]
                entry = {
                    "id":        r["id"],
                    "name":      r["name"] or r["id"] or "Unknown location",
                    "country":   r["country"],
                    "city":      r["city"],
                    "address":   r["address"],
                    "props":     dict(r["props"] or {}),
                    "connected": connected,
                    "degree":    int(r["degree"] or 0),
                }
                lat, lng = r["lat"], r["lng"]
                if lat is not None and lng is not None:
                    try:
                        entry["lat"] = float(lat)
                        entry["lng"] = float(lng)
                        geolocated.append(entry)
                    except (TypeError, ValueError):
                        ungeolocated.append(entry)
                else:
                    ungeolocated.append(entry)
        except Exception as exc:
            log.warning("get_locations error: %s", exc)

    return {
        "locations":    geolocated,
        "ungeolocated": ungeolocated,
        "total":        len(geolocated) + len(ungeolocated),
    }


async def geocode_location(
    graph_db,
    location_id: str,
) -> Optional[dict]:
    """
    Attempt to geocode a Location node via Nominatim (OSM).

    Builds a search query from whatever address fields exist on the node,
    writes the result back to Neo4j, and returns the hit.
    Returns None if the node doesn't exist in the graph.
    Returns {"error": "..."} if geocoding fails.
    """
    # Fetch the node's properties
    async with graph_db.driver.session() as session:
        rec = await (
            await session.run(
                "MATCH (l:Location {id: $id}) RETURN l {.*} AS props LIMIT 1",
                id=location_id,
            )
        ).single()

    if rec is None:
        return None

    props = dict(rec["props"])

    # Build query from most specific → least specific address fields
    parts: list[str] = []
    for field in ("address", "name", "city", "state", "country"):
        v = props.get(field)
        if v and str(v).strip():
            parts.append(str(v).strip())

    if not parts:
        return {"error": "Location has no address fields to geocode", "id": location_id}

    query = ", ".join(parts[:3])   # avoid overly long queries

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                _NOMINATIM,
                params={"q": query, "format": "json", "limit": 1},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            hits = resp.json()
    except Exception as exc:
        log.warning("Nominatim error for %s: %s", location_id, exc)
        return {"error": str(exc), "id": location_id}

    if not hits:
        return {"error": f"No result for query: {query!r}", "id": location_id, "query": query}

    hit  = hits[0]
    lat  = float(hit["lat"])
    lng  = float(hit["lon"])
    gname = hit.get("display_name", query)

    # Persist coordinates to the graph
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (l:Location {id: $id})
            SET l.lat = $lat, l.lng = $lng, l.geocoded_name = $gname
            """,
            id=location_id, lat=lat, lng=lng, gname=gname,
        )

    log.info("Geocoded %s → (%.5f, %.5f) via %r", location_id, lat, lng, query)
    return {
        "id":           location_id,
        "lat":          lat,
        "lng":          lng,
        "geocoded_name": gname,
        "query":        query,
    }


# ── IP geolocation ────────────────────────────────────────────────────────────

_IPAPI_URL  = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,city,regionName,lat,lon,isp,org,query"
_IP_RE      = re.compile(
    r"^("
    r"(\d{1,3}\.){3}\d{1,3}"           # IPv4
    r"|([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}"  # IPv6
    r")$"
)

def _entity_id(label: str, value: str) -> str:
    """Match the deterministic ID scheme used by entity/create."""
    return f"{label.lower()}_{hashlib.sha256(value.lower().encode()).hexdigest()[:16]}"


async def geolocate_ip(graph_db, ip: str) -> dict:
    """
    Geolocate an IP address via ip-api.com (free, no key required).

    Creates a Location node and a HAS_LOCATION edge from the IP entity
    (if it exists in the graph).  Returns the location dict or {"error": "..."}.
    """
    ip = ip.strip()
    if not _IP_RE.match(ip):
        return {"error": f"Invalid IP address: {ip!r}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_IPAPI_URL.format(ip=ip))
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("ip-api.com error for %s: %s", ip, exc)
        return {"error": str(exc)}

    if data.get("status") != "success":
        return {"error": data.get("message", "IP geolocation failed")}

    lat     = float(data["lat"])
    lng     = float(data["lon"])
    country = data.get("country", "")
    city    = data.get("city", "")
    region  = data.get("regionName", "")
    isp     = data.get("isp", "")
    org     = data.get("org", "")

    name   = ", ".join(filter(None, [city, region, country])) or ip
    loc_id = f"location_ip_{re.sub(r'[^a-zA-Z0-9]', '_', ip)}"
    ip_eid = _entity_id("ip", ip)

    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (l:Location {id: $loc_id})
            SET l.name    = $name,
                l.lat     = $lat,
                l.lng     = $lng,
                l.country = $country,
                l.city    = $city,
                l.region  = $region,
                l.isp     = $isp,
                l.org     = $org,
                l.ip      = $ip
            WITH l
            OPTIONAL MATCH (ipn:IP {id: $ip_eid})
            FOREACH (_ IN CASE WHEN ipn IS NOT NULL THEN [1] ELSE [] END |
                MERGE (ipn)-[:HAS_LOCATION]->(l)
            )
            """,
            loc_id=loc_id, name=name, lat=lat, lng=lng,
            country=country, city=city, region=region,
            isp=isp, org=org, ip=ip, ip_eid=ip_eid,
        )

    log.info("Geolocated IP %s → %s (%.4f, %.4f)", ip, name, lat, lng)
    return {
        "id":      loc_id,
        "name":    name,
        "lat":     lat,
        "lng":     lng,
        "country": country,
        "city":    city,
        "region":  region,
        "isp":     isp,
        "org":     org,
        "ip":      ip,
    }


# ── Manual location entry ─────────────────────────────────────────────────────

async def add_manual_location(
    graph_db,
    name: str,
    address: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> dict:
    """
    Add a Location node manually.

    If lat/lng are provided they are used directly.
    Otherwise the name/address is geocoded via Nominatim.
    """
    name    = (name or "").strip()
    address = (address or "").strip()

    if not name and not address:
        return {"error": "Provide at least a name or address"}

    # Geocode if no coordinates supplied
    if lat is None or lng is None:
        query = address or name
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    _NOMINATIM,
                    params={"q": query, "format": "json", "limit": 1},
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                hits = resp.json()
        except Exception as exc:
            return {"error": f"Geocoding failed: {exc}"}

        if not hits:
            return {"error": f"No result found for: {query!r}"}

        lat  = float(hits[0]["lat"])
        lng  = float(hits[0]["lon"])
        if not name:
            name = hits[0].get("display_name", query)

    key    = (name + address).lower().strip()
    loc_id = f"location_{hashlib.sha256(key.encode()).hexdigest()[:16]}"

    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (l:Location {id: $id})
            SET l.name    = $name,
                l.address = $address,
                l.lat     = $lat,
                l.lng     = $lng
            """,
            id=loc_id, name=name, address=address,
            lat=float(lat), lng=float(lng),
        )

    log.info("Manual location added: %s (%.4f, %.4f)", name, lat, lng)
    return {"id": loc_id, "name": name, "lat": float(lat), "lng": float(lng), "address": address}

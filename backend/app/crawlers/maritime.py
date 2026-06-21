"""Maritime / vessel intelligence (free).

Complements OpenSky (aircraft) with ship tracking — valuable for
sanctions and trade-evasion investigations. Resolves a vessel by MMSI,
IMO, or name and returns:

  * Live position + metadata from Digitraffic (free, no key) when the
    vessel has recently been seen (broad coverage via the global AIS feed).
  * Openable tracking links across the major public trackers
    (MarineTraffic, VesselFinder, MyShipTracking, Equasis) — same pattern
    as reverse-image search-link construction.
"""
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("fieldwork.maritime")

_DIGITRAFFIC = "https://meri.digitraffic.fi/api/ais/v1"
_HEADERS = {"User-Agent": "Fieldwork-OSINT/1.0 (research)",
            "Accept": "application/json", "Digitraffic-User": "Fieldwork/OSINT"}

_MMSI_RE = re.compile(r"^\d{9}$")
_IMO_RE  = re.compile(r"^(?:IMO)?\s?\d{7}$", re.I)


def _classify(q: str) -> str:
    s = q.strip()
    if _MMSI_RE.match(s):
        return "mmsi"
    if _IMO_RE.match(s):
        return "imo"
    return "name"


def _tracking_links(q: str, kind: str) -> list:
    term = q.strip().replace("IMO", "").strip()
    enc = httpx.QueryParams({"q": term}).get("q") or term
    links = [
        {"site": "MarineTraffic", "url": f"https://www.marinetraffic.com/en/ais/index/search/all?keyword={enc}"},
        {"site": "VesselFinder",  "url": f"https://www.vesselfinder.com/vessels?name={enc}"},
        {"site": "MyShipTracking", "url": f"https://www.myshiptracking.com/search?type=vessels&q={enc}"},
    ]
    if kind == "imo":
        links.append({"site": "Equasis", "url": "https://www.equasis.org/ (search IMO " + term + ")"})
    return links


async def track_vessel(query: str) -> dict:
    """Resolve a vessel (MMSI/IMO/name) → live position (if available) + tracking links."""
    kind = _classify(query)
    out: dict = {
        "query": query, "identifier_type": kind, "found": False,
        "tracking_links": _tracking_links(query, kind),
    }

    # Live attempt only works with an MMSI (Digitraffic keys on MMSI).
    if kind == "mmsi":
        mmsi = query.strip()
        try:
            async with httpx.AsyncClient(timeout=18.0, headers=_HEADERS) as client:
                loc = await client.get(f"{_DIGITRAFFIC}/locations", params={"mmsi": mmsi})
                meta = await client.get(f"{_DIGITRAFFIC}/vessels/{mmsi}")
                if loc.status_code == 200:
                    feats = loc.json().get("features", [])
                    if feats:
                        f = feats[0]
                        geom = f.get("geometry", {}).get("coordinates", [None, None])
                        props = f.get("properties", {}) or {}
                        ts = props.get("timestampExternal") or props.get("timestamp")
                        out.update({
                            "found": True,
                            "live_position": {
                                "lon": geom[0], "lat": geom[1],
                                "sog_knots": props.get("sog"),
                                "course":    props.get("cog"),
                                "heading":   props.get("heading"),
                                "nav_status": props.get("navStat"),
                                "seen": _ts_ms(ts),
                            },
                        })
                if meta.status_code == 200:
                    m = meta.json()
                    out["vessel"] = {
                        "name":      m.get("name", "").strip(),
                        "mmsi":      m.get("mmsi"),
                        "imo":       m.get("imo"),
                        "callsign":  (m.get("callSign") or "").strip(),
                        "ship_type": m.get("shipType"),
                        "destination": (m.get("destination") or "").strip(),
                        "draught":   m.get("draught"),
                    }
                    out["found"] = out["found"] or bool(out["vessel"]["name"])
        except Exception as exc:
            log.warning("Digitraffic vessel lookup failed for %s: %s", mmsi, exc)
            out["live_error"] = str(exc)
        if not out["found"]:
            out["note"] = "No recent AIS hit in the Digitraffic feed — use the tracking links for global coverage."
    else:
        out["note"] = "Live position requires a 9-digit MMSI; use the tracking links to resolve by name/IMO."

    return out


def _ts_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""

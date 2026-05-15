"""
Aircraft & flight enrichment — Phase 6.1

Two endpoints, both on-demand (not auto-crawl):

  enrich_aircraft_faa(registration)
      Scrape FAA N-Number registry for owner, address, aircraft type.
      Creates an Aircraft node; links it to a Company/Person if owner known.

  enrich_aircraft_flights(graph_db, icao24, days)
      Query OpenSky Network for flight history.
      Creates Flight nodes with origin/destination airports + timestamps.

Usage:
  GET /enrich/aircraft/faa/{registration}       e.g. N12345
  GET /enrich/aircraft/flights/{icao24}         e.g. a835af
  GET /enrich/aircraft/flights/{icao24}?days=14

Authentication:
  OPENSKY_USERNAME + OPENSKY_PASSWORD  (free account at opensky-network.org)
  Without credentials: restricted to 400 req/day, history limited to 1 hour.
"""
import logging
import os
import time
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("crawler.opensky")

_OPENSKY_USER = os.getenv("OPENSKY_USERNAME", "")
_OPENSKY_PASS = os.getenv("OPENSKY_PASSWORD", "")
_OPENSKY_BASE = "https://opensky-network.org/api"
_FAA_BASE     = "https://registry.faa.gov"

_UA = "Mozilla/5.0 (compatible; Fieldwork-OSINT/0.4)"


# ── FAA registry ─────────────────────────────────────────────────────────────

async def enrich_aircraft_faa(graph_db, registration: str) -> dict:
    """
    Scrape the FAA N-Number inquiry page for aircraft owner + model info.
    Stores an Aircraft node; attempts to link owner to an existing Person/Company.
    """
    reg = registration.upper().strip()
    if not reg.startswith("N"):
        reg = f"N{reg}"

    log.info("FAA lookup: %s", reg)
    data = await _scrape_faa(reg)
    if not data:
        return {"found": False, "registration": reg,
                "reason": "Registration not found or FAA site unreachable"}

    # Write Aircraft node
    aircraft_id = f"aircraft:{reg.lower()}"
    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (a:Aircraft {id: $id}) "
            "ON CREATE SET a.registration = $reg, a.manufacturer = $mfr, "
            "              a.model = $model, a.year = $year, "
            "              a.owner = $owner, a.state = $state, "
            "              a.source = 'faa', a.first_seen = datetime() "
            "ON MATCH  SET a.owner = $owner, a.model = $model",
            id=aircraft_id, reg=reg,
            mfr=data.get("manufacturer", ""),
            model=data.get("model", ""),
            year=data.get("year", ""),
            owner=data.get("owner", ""),
            state=data.get("state", ""),
        )

        # Try to link to existing Person or Company by owner name
        owner = data.get("owner", "").strip()
        if owner:
            await session.run(
                "MATCH (a:Aircraft {id: $aid}) "
                "OPTIONAL MATCH (p:Person) WHERE toLower(p.name) = toLower($owner) "
                "OPTIONAL MATCH (c:Company) WHERE toLower(c.name) = toLower($owner) "
                "WITH a, p, c "
                "FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (p)-[:OWNS_AIRCRAFT]->(a)) "
                "FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (c)-[:OWNS_AIRCRAFT]->(a))",
                aid=aircraft_id, owner=owner,
            )

    log.info("FAA: %s → %s / %s owned by %s",
             reg, data.get("manufacturer"), data.get("model"), data.get("owner"))
    return {"found": True, "registration": reg, **data, "aircraft_id": aircraft_id}


async def _scrape_faa(reg: str) -> Optional[dict]:
    """Scrape the FAA N-Number inquiry page and extract key fields."""
    url = f"{_FAA_BASE}/aircraftinquiry/Search/NNumberInquiry"
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            r = await client.get(url, params={"nNumberTxt": reg.lstrip("N")})
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        def _field(label: str) -> str:
            """Find a label cell and return its sibling value cell text."""
            for th in soup.find_all(["th", "td"]):
                if label.lower() in th.get_text(strip=True).lower():
                    sib = th.find_next_sibling("td")
                    if sib:
                        return sib.get_text(strip=True)
            return ""

        # Check for no-results indicator
        if "no aircraft found" in r.text.lower() or "no results" in r.text.lower():
            return None

        return {
            "owner":        _field("Name") or _field("Owner"),
            "street":       _field("Street"),
            "city":         _field("City"),
            "state":        _field("State"),
            "zip":          _field("Zip"),
            "country":      _field("Country"),
            "manufacturer": _field("Manufacturer") or _field("MFR"),
            "model":        _field("Model"),
            "year":         _field("Year Mfr") or _field("Year"),
            "serial":       _field("Serial Number") or _field("Serial"),
            "engine_type":  _field("Engine Type"),
            "aircraft_type": _field("Aircraft Type") or _field("Type"),
            "status":       _field("Certificate Issue") or _field("Status"),
        }
    except Exception as exc:
        log.warning("FAA scrape failed for %s: %s", reg, exc)
        return None


# ── OpenSky flight history ────────────────────────────────────────────────────

async def enrich_aircraft_flights(graph_db, icao24: str, days: int = 7) -> dict:
    """
    Fetch flight history for an aircraft from OpenSky Network.
    Creates Flight nodes with departure/arrival airports and timestamps.
    """
    icao24 = icao24.lower().strip()
    if not re.match(r"^[0-9a-f]{6}$", icao24):
        return {"found": False, "icao24": icao24,
                "reason": "ICAO24 must be exactly 6 hex characters (e.g. a835af)"}

    days = max(1, min(days, 30))
    end   = int(time.time())
    begin = end - days * 86400

    log.info("OpenSky: flights for %s over %d days", icao24, days)
    flights = await _fetch_opensky_flights(icao24, begin, end)

    if flights is None:
        return {"found": False, "icao24": icao24,
                "reason": "OpenSky API unreachable or credentials invalid"}
    if not flights:
        return {"found": True, "icao24": icao24, "flights": [],
                "message": f"No flights recorded in the last {days} days"}

    stored = 0
    async with graph_db.driver.session() as session:
        for f in flights[:100]:
            fid = f"{icao24}:{f.get('firstSeen', '')}"
            await session.run(
                "MERGE (fl:Flight {id: $id}) "
                "ON CREATE SET "
                "  fl.icao24          = $icao24, "
                "  fl.callsign        = $callsign, "
                "  fl.origin_airport  = $origin, "
                "  fl.dest_airport    = $dest, "
                "  fl.departure_time  = $dep, "
                "  fl.arrival_time    = $arr, "
                "  fl.source          = 'opensky', "
                "  fl.first_seen      = datetime() "
                "WITH fl "
                "MERGE (a:Aircraft {id: $aid}) "
                "ON CREATE SET a.icao24 = $icao24, a.source = 'opensky', a.first_seen = datetime() "
                "MERGE (a)-[:OPERATED_FLIGHT]->(fl)",
                id=fid,
                icao24=icao24,
                callsign=(f.get("callsign") or "").strip(),
                origin=f.get("estDepartureAirport") or "",
                dest=f.get("estArrivalAirport") or "",
                dep=f.get("firstSeen") or 0,
                arr=f.get("lastSeen") or 0,
                aid=f"aircraft:{icao24}",
            )
            stored += 1

    # Summarise unique routes
    routes = list({
        f"{f.get('estDepartureAirport','?')} → {f.get('estArrivalAirport','?')}"
        for f in flights if f.get("estDepartureAirport") or f.get("estArrivalAirport")
    })

    log.info("OpenSky: %d flights stored for %s", stored, icao24)
    return {
        "found":   True,
        "icao24":  icao24,
        "days":    days,
        "flights": stored,
        "routes":  sorted(routes),
        "raw":     flights[:20],   # first 20 for UI display
    }


async def _fetch_opensky_flights(icao24: str, begin: int, end: int) -> Optional[list]:
    """Hit OpenSky /flights/aircraft with optional basic-auth credentials."""
    params = {"icao24": icao24, "begin": begin, "end": end}
    auth   = (_OPENSKY_USER, _OPENSKY_PASS) if _OPENSKY_USER else None

    try:
        async with httpx.AsyncClient(timeout=20.0, auth=auth) as client:
            r = await client.get(f"{_OPENSKY_BASE}/flights/aircraft", params=params)

        if r.status_code == 401:
            log.warning("OpenSky: bad credentials")
            return None
        if r.status_code == 404:
            return []   # no flights found
        if r.status_code != 200:
            log.warning("OpenSky: HTTP %s", r.status_code)
            return None

        return r.json()
    except Exception as exc:
        log.warning("OpenSky request failed: %s", exc)
        return None

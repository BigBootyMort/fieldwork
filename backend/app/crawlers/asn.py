"""
Phase 32 — ASN / BGP enrichment for IP addresses.

Data source: BGPView API (https://bgpview.io)
  • Free, no authentication required
  • Returns: ASN number, name, description, prefix, country, RIR registry
  • Rate limit: ~45 requests/minute (generous for investigative use)

Creates ASN nodes in the graph and links them to IP nodes via
BELONGS_TO_ASN relationships. This surfaces IP ownership and
routing infrastructure context that Shodan/VT don't provide.
"""
import logging
import os
import httpx

log = logging.getLogger("fieldwork.asn")

_BGPVIEW_TIMEOUT = 15
_IPAPI_TIMEOUT   = 8

# ip-api.com as fallback — free, 45 req/min, no key
_IPAPI_FIELDS = "status,country,countryCode,region,regionName,city,isp,org,as,asname,query"


def _asn_node_id(asn: int) -> str:
    return f"asn_{asn}"


def _ip_node_id(ip: str) -> str:
    import hashlib
    return f"ip_{hashlib.sha1(ip.encode()).hexdigest()[:12]}"


async def _bgpview_lookup(ip: str) -> dict | None:
    """Call BGPView /ip/{ip} and return parsed data or None on failure."""
    url = f"https://api.bgpview.io/ip/{ip}"
    try:
        async with httpx.AsyncClient(timeout=_BGPVIEW_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Fieldwork OSINT"})
            if r.status_code != 200:
                return None
            body = r.json()
            if body.get("status") != "ok":
                return None
            data = body.get("data", {})

            # Pull first prefix/ASN entry
            prefixes = data.get("prefixes", [])
            if not prefixes:
                return None

            pref  = prefixes[0]
            asn_d = pref.get("asn", {})

            return {
                "asn":         asn_d.get("asn"),
                "asn_name":    asn_d.get("name", ""),
                "description": asn_d.get("description", ""),
                "prefix":      pref.get("prefix", ""),
                "country":     pref.get("country_code", ""),
                "rir":         pref.get("rir_allocation", {}).get("rir_name", ""),
            }
    except Exception as exc:
        log.debug("BGPView lookup failed for %s: %s", ip, exc)
        return None


async def _ipapi_lookup(ip: str) -> dict | None:
    """Fallback: ip-api.com — also returns city/ISP/org."""
    url = f"http://ip-api.com/json/{ip}?fields={_IPAPI_FIELDS}"
    try:
        async with httpx.AsyncClient(timeout=_IPAPI_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Fieldwork OSINT"})
            r.raise_for_status()
            d = r.json()
            if d.get("status") != "success":
                return None

            raw_as = d.get("as", "")   # e.g. "AS15169 Google LLC"
            asn_num = None
            if raw_as.startswith("AS"):
                try:
                    asn_num = int(raw_as.split()[0][2:])
                except ValueError:
                    pass

            return {
                "asn":         asn_num,
                "asn_name":    d.get("asname", d.get("org", "")),
                "description": d.get("isp", ""),
                "prefix":      "",
                "country":     d.get("countryCode", ""),
                "rir":         "",
                "city":        d.get("city", ""),
                "region":      d.get("regionName", ""),
                "isp":         d.get("isp", ""),
                "org":         d.get("org", ""),
            }
    except Exception as exc:
        log.debug("ip-api fallback failed for %s: %s", ip, exc)
        return None


async def enrich_ip_asn(graph_db, ip: str) -> dict:
    """
    Enrich an IP node with BGP/ASN ownership data.

    Tries BGPView first, falls back to ip-api.com.
    Persists:
      • IP node properties: asn, asn_name, isp, country, prefix
      • ASN node: id=asn_{N}, number, name, description, country
      • Relationship: (IP)-[:BELONGS_TO_ASN]->(ASN)

    Returns the enriched dict.
    """
    # Try BGPView first, then ip-api
    data = await _bgpview_lookup(ip) or await _ipapi_lookup(ip)

    if not data:
        return {
            "ip":    ip,
            "error": "No ASN data found (BGPView and ip-api both failed)",
        }

    asn_num     = data.get("asn")
    asn_name    = data.get("asn_name", "")
    description = data.get("description", "")
    prefix      = data.get("prefix", "")
    country     = data.get("country", "")
    rir         = data.get("rir", "")
    isp         = data.get("isp", "")
    org         = data.get("org", "")

    ip_nid = _ip_node_id(ip)

    async with graph_db.driver.session() as session:
        # Update IP node properties
        await session.run("""
            MERGE (n:IP {id: $id})
            ON CREATE SET n.ip = $ip, n.address = $ip, n.created_at = datetime()
            SET n.asn        = $asn,
                n.asn_name   = $asn_name,
                n.isp        = $isp,
                n.org        = $org,
                n.prefix     = $prefix,
                n.country    = coalesce(n.country, $country)
        """,
            id=ip_nid, ip=ip,
            asn=str(asn_num) if asn_num else "",
            asn_name=asn_name, isp=isp or description,
            org=org, prefix=prefix, country=country,
        )

        # Create ASN node and relationship (only if we have a real ASN number)
        if asn_num:
            asn_nid = _asn_node_id(asn_num)
            await session.run("""
                MERGE (a:ASN {id: $id})
                ON CREATE SET a.number      = $num,
                              a.name        = $name,
                              a.description = $desc,
                              a.country     = $country,
                              a.rir         = $rir,
                              a.created_at  = datetime()
                ON MATCH  SET a.name        = $name,
                              a.description = $desc
            """,
                id=asn_nid, num=asn_num, name=asn_name,
                desc=description, country=country, rir=rir,
            )
            await session.run("""
                MATCH (i:IP {id: $iid}), (a:ASN {id: $aid})
                MERGE (i)-[:BELONGS_TO_ASN]->(a)
            """, iid=ip_nid, aid=asn_nid)

    return {
        "ip":          ip,
        "asn":         asn_num,
        "asn_name":    asn_name,
        "description": description,
        "prefix":      prefix,
        "country":     country,
        "rir":         rir,
        "isp":         isp or description,
        "org":         org,
        "source":      "bgpview" if prefix else "ipapi",
    }

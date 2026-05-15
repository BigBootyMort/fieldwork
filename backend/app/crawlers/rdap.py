"""
RDAP domain-enrichment helper.

Called on-demand via GET /enrich/domain/{domain} rather than
automatically during the crawl pipeline — a single crt.sh crawl can
surface 50 domains, and enriching all of them would be slow and noisy.

Uses the rdap.org public proxy which handles per-TLD RDAP routing so
we don't need a registrar-specific endpoint for each domain.

Privacy shields (WhoisGuard, Domains By Proxy, etc.) mean registrant
fields are often redacted. We store what we get and skip empty fields.
"""
import httpx
import logging
from typing import Optional

log = logging.getLogger("rdap")

RDAP_BASE = "https://rdap.org/domain"


def _vcard_field(vcard_array: list, field: str) -> Optional[str]:
    """Extract a single text field from a vCard 4.0 array."""
    for entry in vcard_array:
        if isinstance(entry, list) and len(entry) >= 4 and entry[0] == field:
            value = entry[3]
            if isinstance(value, list):
                # adr fields come as structured arrays; join non-empty parts
                return ", ".join(p for p in value if p)
            return str(value) if value else None
    return None


async def enrich_domain(graph_db, domain: str) -> dict:
    """
    Query RDAP for `domain`, update the Domain node in Neo4j, and
    create linked nodes (Person/Company/Location) from registrant data.

    Returns a summary dict with whatever was found.
    """
    async with httpx.AsyncClient(
        timeout=20.0,
        headers={"User-Agent": "fieldwork-osint/0.2", "Accept": "application/rdap+json"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(f"{RDAP_BASE}/{domain}")

    if resp.status_code == 404:
        return {"domain": domain, "found": False, "reason": "not found in RDAP"}
    if resp.status_code != 200:
        return {"domain": domain, "found": False, "reason": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"domain": domain, "found": False, "reason": "non-JSON response"}

    result: dict = {"domain": domain, "found": True}

    # Registration / expiry dates
    for event in data.get("events", []):
        action = event.get("eventAction", "")
        date = event.get("eventDate", "")
        if action == "registration":
            result["registered"] = date
        elif action == "expiration":
            result["expires"] = date
        elif action == "last changed":
            result["updated"] = date

    # Nameservers
    result["nameservers"] = [
        ns.get("ldhName", "").lower()
        for ns in data.get("nameservers", [])
        if ns.get("ldhName")
    ]

    # Registrant entity
    registrant_name: Optional[str] = None
    registrant_org: Optional[str] = None
    registrant_email: Optional[str] = None
    registrant_address: Optional[str] = None
    registrant_country: Optional[str] = None

    for entity in data.get("entities", []):
        roles = entity.get("roles", [])
        if "registrant" not in roles:
            continue
        vcard_raw = entity.get("vcardArray", [])
        # vcardArray is ["vcard", [[field, params, type, value], ...]]
        vcard = vcard_raw[1] if isinstance(vcard_raw, list) and len(vcard_raw) >= 2 else []

        registrant_name = _vcard_field(vcard, "fn")
        registrant_org = _vcard_field(vcard, "org")
        registrant_email = _vcard_field(vcard, "email")
        registrant_address = _vcard_field(vcard, "adr")
        registrant_country = _vcard_field(vcard, "country-name")
        break  # take first registrant only

    result.update({
        "registrant_name": registrant_name,
        "registrant_org": registrant_org,
        "registrant_email": registrant_email,
        "registrant_country": registrant_country,
    })

    # Persist to Neo4j
    async with graph_db.driver.session() as session:
        # Enrich the Domain node itself
        await session.run(
            "MERGE (d:Domain {id: $id}) "
            "ON CREATE SET d.name = $id, d.first_seen = datetime() "
            "SET d.registered = $registered, d.expires = $expires, "
            "    d.registrant_name = $rname, d.registrant_org = $rorg, "
            "    d.registrant_email = $remail, d.registrant_country = $rcountry, "
            "    d.nameservers = $ns, d.rdap_fetched = datetime()",
            id=domain,
            registered=result.get("registered", ""),
            expires=result.get("expires", ""),
            rname=registrant_name or "",
            rorg=registrant_org or "",
            remail=registrant_email or "",
            rcountry=registrant_country or "",
            ns=result["nameservers"],
        )

        # Registrant org → Company node
        if registrant_org and not _looks_like_privacy_shield(registrant_org):
            await session.run(
                "MERGE (c:Company {id: $id}) "
                "ON CREATE SET c.name = $name, c.created_at = datetime() "
                "WITH c "
                "MATCH (d:Domain {id: $did}) "
                "MERGE (c)-[r:OWNS_DOMAIN]->(d) "
                "ON CREATE SET r.source = 'rdap', r.first_seen = datetime()",
                id=_simple_slug(registrant_org),
                name=registrant_org,
                did=domain,
            )

        # Registrant country → Location node
        if registrant_country:
            await session.run(
                "MERGE (l:Location {id: $id}) "
                "ON CREATE SET l.name = $name, l.first_seen = datetime() "
                "WITH l "
                "MATCH (d:Domain {id: $did}) "
                "MERGE (d)-[r:IN_COUNTRY]->(l) "
                "ON CREATE SET r.source = 'rdap', r.first_seen = datetime()",
                id=_simple_slug(registrant_country),
                name=registrant_country,
                did=domain,
            )

    log.info("RDAP enriched %s: org=%r country=%r", domain, registrant_org, registrant_country)
    return result


# Privacy-shield strings that are meaningless as company names
_PRIVACY_KEYWORDS = {"privacy", "whoisguard", "withheld", "redacted", "proxy", "protect"}


def _looks_like_privacy_shield(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in _PRIVACY_KEYWORDS)


def _simple_slug(text: str) -> str:
    import re, unicodedata
    n = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")[:180] or "unknown"

"""
Phone Number Intelligence — parse, validate, and enrich phone number entities.

Uses the `phonenumbers` library (covers 250+ countries, always up to date) for:
  - E.164 validation and formatting
  - Country / region of registration
  - Carrier name (where available from number blocks)
  - Line type (MOBILE, FIXED_LINE, VOIP, TOLL_FREE, PREMIUM_RATE, etc.)
  - Timezone(s) for the number

Also generates investigation resource links (no API key required):
  - Truecaller web search
  - Spy Dialer
  - Sync.me
  - Google search for the number

Optional NumVerify lookup (NUMVERIFY_KEY in env) adds live carrier and line-type
data for numbers not covered by the offline library.

Updates or creates a Phone node in Neo4j with all enriched properties.
"""
import os
import logging
import hashlib
import httpx

log = logging.getLogger("fieldwork.phone_intel")

try:
    import phonenumbers
    from phonenumbers import (
        carrier as pn_carrier,
        geocoder as pn_geocoder,
        timezone as pn_timezone,
        PhoneNumberType,
    )
    _HAS_LIB = True
except ImportError:
    _HAS_LIB = False
    log.warning("phonenumbers library not installed — phone parsing unavailable")

_NUMVERIFY_KEY = os.getenv("NUMVERIFY_KEY", "")

# Human-readable line-type labels
_TYPE_LABELS = {
    0:  "FIXED_LINE",
    1:  "MOBILE",
    2:  "FIXED_OR_MOBILE",
    3:  "TOLL_FREE",
    4:  "PREMIUM_RATE",
    5:  "SHARED_COST",
    6:  "VOIP",
    7:  "PERSONAL_NUMBER",
    8:  "PAGER",
    9:  "UAN",
    10: "VOICEMAIL",
    99: "UNKNOWN",
}

# Investigation resource templates (filled with E.164 or local number)
def _investigation_links(e164: str, national: str) -> list[dict]:
    bare = e164.lstrip("+").replace(" ", "")
    return [
        {"name": "Truecaller",      "url": f"https://www.truecaller.com/search/us/{national}"},
        {"name": "Spy Dialer",      "url": f"https://www.spydialer.com/default.aspx?phone={national}"},
        {"name": "Sync.me",         "url": f"https://sync.me/search/?number={e164}"},
        {"name": "Google",          "url": f"https://www.google.com/search?q=%22{national}%22"},
        {"name": "WhoCalledMe",     "url": f"https://www.whocalledme.com/PhoneNumber/{bare}"},
        {"name": "800notes",        "url": f"https://800notes.com/Phone.aspx/{bare}"},
    ]


async def enrich_phone(graph_db, raw_number: str) -> dict:
    """
    Parse and enrich a phone number.  Accepts most common formats:
      +1 (555) 123-4567 · +44 7911 123456 · 07911123456 · 15551234567

    Returns structured data and updates the Phone node in Neo4j.
    """
    raw = raw_number.strip()
    result: dict = {
        "raw":         raw,
        "valid":       False,
        "e164":        None,
        "national":    None,
        "country":     None,
        "country_code":None,
        "region":      None,
        "carrier":     None,
        "line_type":   None,
        "timezones":   [],
        "links":       [],
        "numverify":   None,
        "error":       None,
    }

    if not _HAS_LIB:
        result["error"] = "phonenumbers library not installed"
        return result

    # ── Parse ────────────────────────────────────────────────────────────────
    parsed = None
    for region in (None, "US", "GB"):
        try:
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed):
                break
        except phonenumbers.NumberParseException:
            continue

    if parsed is None or not phonenumbers.is_valid_number(parsed):
        result["error"] = "Could not parse as a valid phone number"
        return result

    result["valid"]        = True
    result["e164"]         = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    result["national"]     = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    result["country_code"] = parsed.country_code
    result["region"]       = phonenumbers.region_code_for_number(parsed)

    # Country name
    try:
        result["country"] = pn_geocoder.description_for_number(parsed, "en") or result["region"]
    except Exception:
        result["country"] = result["region"]

    # Carrier (offline lookup — may be empty for number-portability countries)
    try:
        carrier_name = pn_carrier.name_for_number(parsed, "en")
        result["carrier"] = carrier_name or None
    except Exception:
        pass

    # Line type
    try:
        num_type = phonenumbers.number_type(parsed)
        result["line_type"] = _TYPE_LABELS.get(int(num_type), "UNKNOWN")
    except Exception:
        result["line_type"] = "UNKNOWN"

    # Timezones
    try:
        result["timezones"] = list(pn_timezone.time_zones_for_number(parsed))
    except Exception:
        pass

    # Investigation links
    result["links"] = _investigation_links(result["e164"], result["national"])

    # ── Optional: NumVerify live lookup ──────────────────────────────────────
    if _NUMVERIFY_KEY:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "http://apilayer.net/api/validate",
                    params={
                        "access_key": _NUMVERIFY_KEY,
                        "number":     result["e164"],
                        "country_code": result["region"],
                        "format":     "1",
                    },
                )
                if resp.status_code == 200:
                    nv = resp.json()
                    if nv.get("valid"):
                        result["numverify"] = {
                            "carrier":   nv.get("carrier"),
                            "line_type": nv.get("line_type"),
                            "location":  nv.get("location"),
                        }
                        # Prefer live data over offline library
                        if nv.get("carrier"):
                            result["carrier"]   = nv["carrier"]
                        if nv.get("line_type"):
                            result["line_type"] = nv["line_type"].upper()
        except Exception as exc:
            log.debug("NumVerify lookup failed: %s", exc)

    # ── Upsert Phone node in Neo4j ───────────────────────────────────────────
    node_id = f"phone_{hashlib.sha256(result['e164'].encode()).hexdigest()[:16]}"
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (n:Phone {id: $id})
            ON CREATE SET
                n.number       = $e164,
                n.national     = $national,
                n.country      = $country,
                n.country_code = $cc,
                n.region       = $region,
                n.source       = 'phone_intel',
                n.created_at   = datetime()
            ON MATCH SET
                n.national     = $national,
                n.country      = $country,
                n.country_code = $cc,
                n.region       = $region
            SET n.carrier      = $carrier,
                n.line_type    = $line_type,
                n.timezones    = $tz,
                n.updated_at   = datetime()
            """,
            id=node_id,
            e164=result["e164"],
            national=result["national"],
            country=result["country"] or "",
            cc=str(result["country_code"]),
            region=result["region"] or "",
            carrier=result["carrier"] or "",
            line_type=result["line_type"] or "",
            tz=result["timezones"],
        )

    result["node_id"] = node_id
    return result

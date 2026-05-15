"""
Phase 19 — Bulk entity import.

Parses CSV / JSON / plain-text IOC lists and batch-MERGEs entity nodes
into the graph, optionally linking them to a case as subjects.

Supported input formats
-----------------------
CSV  — columns: type,value[,note]  or  value,type[,note]  or  value (auto-detect)
JSON — list of {type, value, note?} objects, OR a flat list of strings
TXT  — one value per line; lines starting with # or // are skipped
"""
import csv
import hashlib
import io
import json
import logging
import re
from typing import Optional

log = logging.getLogger("fieldwork.bulk_import")

# ── Supported labels & their primary properties ───────────────────────────────

SUPPORTED_LABELS: set[str] = {
    "Person", "Company", "Email", "Phone", "Username",
    "IP", "Domain", "URL", "Wallet", "Location",
}

_ENTITY_PROP: dict[str, str] = {
    "Person":   "name",
    "Company":  "name",
    "Email":    "address",
    "Phone":    "number",
    "Username": "handle",
    "IP":       "address",
    "Domain":   "name",
    "URL":      "url",
    "Wallet":   "address",
    "Location": "name",
}

# ── Label canonicalisation ────────────────────────────────────────────────────

_LABEL_ALIASES: dict[str, str] = {
    # Person
    "person": "Person", "name": "Person", "individual": "Person",
    "suspect": "Person", "target": "Person", "subject": "Person",
    # Company
    "company": "Company", "organization": "Company", "organisation": "Company",
    "org": "Company", "corp": "Company", "business": "Company", "employer": "Company",
    # Email
    "email": "Email", "emailaddress": "Email", "mail": "Email", "e-mail": "Email",
    # Phone
    "phone": "Phone", "telephone": "Phone", "mobile": "Phone",
    "cell": "Phone", "number": "Phone", "tel": "Phone",
    # Username
    "username": "Username", "handle": "Username", "user": "Username",
    "nick": "Username", "nickname": "Username", "alias": "Username",
    # IP
    "ip": "IP", "ipaddress": "IP", "ipaddr": "IP", "ipadress": "IP",
    "ip_address": "IP", "ip4": "IP", "ip6": "IP", "ipv4": "IP", "ipv6": "IP",
    # Domain
    "domain": "Domain", "hostname": "Domain", "host": "Domain",
    "fqdn": "Domain", "subdomain": "Domain",
    # URL
    "url": "URL", "link": "URL", "website": "URL", "site": "URL",
    "webpage": "URL", "uri": "URL",
    # Wallet
    "wallet": "Wallet", "cryptoaddress": "Wallet", "crypto": "Wallet",
    "btc": "Wallet", "eth": "Wallet", "address": "Wallet",
    "btcaddress": "Wallet", "ethaddress": "Wallet",
    # Location
    "location": "Location", "place": "Location", "city": "Location",
    "country": "Location", "region": "Location", "geo": "Location",
}


def _canon_label(raw: str) -> Optional[str]:
    """Resolve a user-supplied type string to a canonical label, or None."""
    key = raw.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in _LABEL_ALIASES:
        return _LABEL_ALIASES[key]
    # Direct match after title-casing
    title = raw.strip().title()
    if title in SUPPORTED_LABELS:
        return title
    return None


# ── Auto-detection ────────────────────────────────────────────────────────────

_RE_EMAIL  = re.compile(r'^[\w.+%-]+@[\w.-]+\.\w{2,}$', re.I)
_RE_IP4    = re.compile(r'^(?:\d{1,3}\.){3}\d{1,3}$')
_RE_IP6    = re.compile(r'^[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}$', re.I)
_RE_URL    = re.compile(r'^https?://', re.I)
_RE_BTC    = re.compile(r'^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$|^bc1[a-z0-9]{39,59}$')
_RE_ETH    = re.compile(r'^0x[a-fA-F0-9]{40}$')
_RE_DOMAIN = re.compile(r'^(?!-)[\w-]{1,63}(?:\.(?!-)[\w-]{1,63})+$', re.I)
_RE_PHONE  = re.compile(r'^\+?\d[\d\s\-().]{5,18}\d$')


def detect_type(value: str) -> Optional[str]:
    """Best-guess entity label for a raw string value."""
    v = value.strip()
    if _RE_EMAIL.match(v):   return "Email"
    if _RE_IP4.match(v):     return "IP"
    if _RE_IP6.match(v):     return "IP"
    if _RE_URL.match(v):     return "URL"
    if _RE_BTC.match(v):     return "Wallet"
    if _RE_ETH.match(v):     return "Wallet"
    if _RE_DOMAIN.match(v):  return "Domain"
    if _RE_PHONE.match(v):   return "Phone"
    return None


# ── File parsers ──────────────────────────────────────────────────────────────

_MAX_ROWS = 5_000

# Row dict exchanged internally
ParsedRow = dict  # keys: type (str), value (str), note (str)


def _parse_csv(data: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Parse CSV with or without a header row.

    Accepted column arrangements (order determined from header or first row):
      • type, value [, note]
      • value, type [, note]
      • value only   (type auto-detected)
    """
    warnings: list[str] = []
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        return [], [f"Encoding error: {exc}"]

    # Sniff delimiter
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)

    rows: list[ParsedRow] = []
    col_type: Optional[int]  = None
    col_value: Optional[int] = None
    col_note: Optional[int]  = None
    header_done = False

    for i, raw in enumerate(reader):
        if i >= _MAX_ROWS + 1:
            warnings.append(f"Truncated at {_MAX_ROWS} rows")
            break

        cells = [c.strip() for c in raw]
        if not any(cells):
            continue

        if not header_done:
            header_done = True
            lower = [c.lower() for c in cells]
            is_header = any(h in lower for h in (
                "type", "label", "entity_type", "entity", "value",
                "ioc", "data", "note", "notes",
            ))
            if is_header:
                for j, h in enumerate(lower):
                    if h in ("type", "label", "entity_type"):
                        col_type = j
                    elif h in ("value", "entity", "data", "ioc", "indicator"):
                        col_value = j
                    elif h in ("note", "notes", "comment", "description"):
                        col_note = j
                if col_value is None:
                    col_value = 0 if col_type != 0 else 1
                continue  # skip header row
            else:
                # No header — guess layout from first data row
                first = cells[0].lower()
                if first in _LABEL_ALIASES or cells[0].title() in SUPPORTED_LABELS:
                    col_type = 0; col_value = 1
                    col_note = 2 if len(cells) > 2 else None
                else:
                    col_value = 0
                    col_type  = 1 if len(cells) > 1 else None
                    col_note  = 2 if len(cells) > 2 else None
                # Fall through to process this row as data

        def _get(idx: Optional[int]) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].strip()

        value = _get(col_value)
        typ   = _get(col_type)
        note  = _get(col_note)

        if value:
            rows.append({"type": typ, "value": value, "note": note})

    return rows, warnings


def _parse_json(data: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Parse JSON array of objects or strings."""
    warnings: list[str] = []
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return [], [f"JSON parse error: {exc}"]

    # Unwrap common envelope patterns
    if isinstance(payload, dict):
        for key in ("entities", "items", "data", "results", "iocs", "indicators"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break

    if not isinstance(payload, list):
        return [], ["JSON must be an array (or an object with an 'entities' key containing an array)"]

    rows: list[ParsedRow] = []
    for i, item in enumerate(payload):
        if i >= _MAX_ROWS:
            warnings.append(f"Truncated at {_MAX_ROWS} items")
            break
        if isinstance(item, str):
            v = item.strip()
            if v:
                rows.append({"type": "", "value": v, "note": ""})
        elif isinstance(item, dict):
            value = str(
                item.get("value") or item.get("entity") or
                item.get("data")  or item.get("ioc")    or
                item.get("indicator") or ""
            ).strip()
            typ  = str(
                item.get("type") or item.get("label") or
                item.get("entity_type") or ""
            ).strip()
            note = str(
                item.get("note") or item.get("notes") or
                item.get("description") or item.get("comment") or ""
            ).strip()
            if value:
                rows.append({"type": typ, "value": value, "note": note})
        else:
            warnings.append(f"Row {i}: skipped (not a string or object)")

    return rows, warnings


def _parse_txt(data: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Parse plain text — one value per line."""
    warnings: list[str] = []
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return [], [f"Encoding error: {exc}"]

    rows: list[ParsedRow] = []
    for i, line in enumerate(text.splitlines()):
        if i >= _MAX_ROWS:
            warnings.append(f"Truncated at {_MAX_ROWS} lines")
            break
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("//"):
            continue
        rows.append({"type": "", "value": value, "note": ""})

    return rows, warnings


def parse_file(filename: str, data: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Dispatch to the right parser by file extension, with auto-fallback."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "csv":
        return _parse_csv(data)
    if ext == "json":
        return _parse_json(data)
    if ext in ("txt", "ioc", "log", "list", "text", ""):
        return _parse_txt(data)
    if ext in ("tsv",):
        return _parse_csv(data)

    # Unknown extension — try each parser in order
    for parser in (_parse_json, _parse_csv, _parse_txt):
        rows, warns = parser(data)
        if rows:
            return rows, warns
    return [], ["Could not detect file format — use .csv, .json, or .txt"]


# ── Normalisation ─────────────────────────────────────────────────────────────

def normalise_rows(raw: list[ParsedRow]) -> tuple[list[ParsedRow], list[str]]:
    """Resolve labels, auto-detect where missing, deduplicate within the batch.

    Returns (valid_rows, error_messages).
    """
    valid: list[ParsedRow] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    for i, row in enumerate(raw):
        value = row.get("value", "").strip()
        typ   = row.get("type", "").strip()

        if not value:
            continue

        if typ:
            label = _canon_label(typ)
            if label is None:
                errors.append(
                    f"Row {i+1}: unknown type '{typ[:30]}' for '{value[:40]}' "
                    f"— use one of: {', '.join(sorted(SUPPORTED_LABELS))}"
                )
                continue
        else:
            label = detect_type(value)
            if label is None:
                errors.append(
                    f"Row {i+1}: cannot auto-detect type for '{value[:60]}' "
                    f"— add a 'type' column with the entity label"
                )
                continue

        key = (label, value.lower())
        if key in seen:
            continue
        seen.add(key)

        valid.append({"type": label, "value": value, "note": row.get("note", "")})

    return valid, errors


# ── Graph import ──────────────────────────────────────────────────────────────

def _entity_id(label: str, value: str) -> str:
    """Deterministic ID matching the existing entity/create endpoint."""
    return f"{label.lower()}_{hashlib.sha256(value.lower().encode()).hexdigest()[:16]}"


async def bulk_import_entities(
    graph_db,
    rows: list[ParsedRow],
    case_id: Optional[str] = None,
    source: str = "bulk_import",
) -> dict:
    """MERGE entity nodes from normalised rows; optionally link each to a case.

    Returns a summary dict: created, updated, linked, errors, nodes.
    """
    created = updated = linked = 0
    errors: list[str] = []
    nodes:  list[dict] = []

    async with graph_db.driver.session() as session:
        for row in rows:
            label = row["type"]
            value = row["value"]
            note  = row.get("note", "")
            eid   = _entity_id(label, value)
            prop  = _ENTITY_PROP.get(label, "name")

            # 1 — MERGE the entity node
            try:
                result = await session.run(
                    f"""
                    MERGE (e:{label} {{id: $id}})
                    ON CREATE SET
                        e.{prop}  = $value,
                        e.source  = $source,
                        e.note    = $note,
                        e.created = timestamp()
                    ON MATCH SET
                        e.updated = timestamp()
                    RETURN e.id AS eid,
                           (e.updated IS NULL) AS is_new
                    """,
                    id=eid, value=value, source=source, note=note,
                )
                rec = await result.single()
                if rec and rec["is_new"]:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append(f"MERGE {label}:{value[:40]} — {exc}")
                log.warning("bulk_import MERGE error: %s", exc)
                continue

            nodes.append({"id": eid, "label": label, "value": value})

            # 2 — Link to case
            if case_id:
                try:
                    await session.run(
                        f"""
                        MATCH (c:Case {{id: $cid}})
                        MATCH (e:{label} {{id: $eid}})
                        MERGE (c)-[r:HAS_SUBJECT]->(e)
                        ON CREATE SET
                            r.role         = 'unknown',
                            r.entity_label = $lbl,
                            r.added_at     = datetime()
                        """,
                        cid=case_id, eid=eid, lbl=label,
                    )
                    linked += 1
                except Exception as exc:
                    errors.append(f"Link {eid} to case: {exc}")
                    log.warning("bulk_import link error: %s", exc)

    return {
        "created": created,
        "updated": updated,
        "linked":  linked,
        "errors":  errors,
        "nodes":   nodes[:500],
    }

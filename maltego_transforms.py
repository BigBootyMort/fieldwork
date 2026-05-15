#!/usr/bin/env python3
"""
Fieldwork → Maltego local transforms.

Add these as LOCAL transforms in Maltego CE (free) by pointing Maltego
at this script. Each transform reads a MaltegoMessage from stdin and
writes a MaltegoMessage response to stdout.

Setup in Maltego CE:
  1. Transform Manager → New Local Transform
  2. Command:  python  (or full path to your Python executable)
  3. Parameters: C:\\path\\to\\maltego_transforms.py <TransformName>
  4. Working directory: (any)

Available transform names (pass as first CLI argument):
  PersonToConnections   — Person entity → Company + co-person nodes
  PersonToAleph         — Person entity → OCCRP Aleph hits
  DomainToIPs           — Domain entity → IP nodes (VirusTotal passive DNS)
  DomainToHistory       — Domain entity → Wayback first/last seen
  IPToShodan            — IP entity → ports, org, location

The backend must be running at http://localhost:8000.
"""
import json
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from typing import Optional

BACKEND = "http://localhost:8000"


# ── Maltego XML helpers ───────────────────────────────────────────────────────

def _parse_request(xml_str: str) -> tuple[str, str]:
    """Return (entity_type, entity_value) from a MaltegoMessage request."""
    root = ET.fromstring(xml_str)
    entity = root.find(".//Entity")
    if entity is None:
        return "", ""
    return entity.get("Type", ""), (entity.findtext("Value") or "").strip()


def _build_response(entities: list[dict]) -> str:
    """
    Build a MaltegoMessage XML response.

    Each entity dict must have:
      type  — Maltego entity type string e.g. "maltego.Company"
      value — display value
    Optional:
      weight      — integer relevance weight
      fields      — list of {"name": ..., "display": ..., "value": ...}
    """
    root = ET.Element("MaltegoMessage")
    msg  = ET.SubElement(root, "MaltegoTransformResponseMessage")
    ET.SubElement(msg, "UIMessages")
    ents = ET.SubElement(msg, "Entities")

    for e in entities:
        ent = ET.SubElement(ents, "Entity", Type=e["type"])
        ET.SubElement(ent, "Value").text = str(e["value"])
        ET.SubElement(ent, "Weight").text = str(e.get("weight", 100))
        if e.get("fields"):
            af = ET.SubElement(ent, "AdditionalFields")
            for f in e["fields"]:
                field = ET.SubElement(af, "Field",
                    Name=f["name"], DisplayName=f.get("display", f["name"]))
                field.text = str(f.get("value", ""))

    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _api(path: str, method: str = "GET", body: Optional[dict] = None) -> Optional[dict]:
    """Simple synchronous HTTP call to the Fieldwork backend."""
    url = BACKEND + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        _ui_error(f"Backend unreachable: {e}")
        return None
    except Exception as e:
        _ui_error(str(e))
        return None


def _ui_error(msg: str) -> None:
    """Print a Maltego UIMessage error to stderr (shown in Maltego's log)."""
    sys.stderr.write(f"[Fieldwork] {msg}\n")


# ── Transforms ────────────────────────────────────────────────────────────────

def transform_person_to_connections(value: str) -> list[dict]:
    data = _api("/crawl/person", "POST", {"name": value})
    if not data:
        return []
    entities = []
    for conn in data.get("connections", []):
        e = conn.get("entity", {})
        labels = e.get("labels", [])
        name   = e.get("name") or e.get("id", "")
        if not name:
            continue
        if "Company" in labels:
            entities.append({"type": "maltego.Company", "value": name,
                             "weight": max(1, 100 - conn.get("distance", 1) * 20)})
        elif "Person" in labels:
            entities.append({"type": "maltego.Person", "value": name,
                             "weight": max(1, 100 - conn.get("distance", 1) * 20)})
    return entities


def transform_person_to_aleph(value: str) -> list[dict]:
    data = _api(f"/search/aleph?q={urllib.request.quote(value)}")
    if not data:
        return []
    entities = []
    for hit in data.get("results", [])[:20]:
        caption = hit.get("caption", "")
        schema  = hit.get("schema", "")
        if not caption:
            continue
        mtype = "maltego.Company" if schema in ("Company", "Organization", "PublicBody") else "maltego.Person"
        datasets = ", ".join(hit.get("datasets", []))[:100]
        entities.append({
            "type": mtype, "value": caption, "weight": 80,
            "fields": [{"name": "source", "display": "Source", "value": datasets}],
        })
    return entities


def transform_domain_to_ips(value: str) -> list[dict]:
    data = _api(f"/enrich/domain/{value}/vt")
    if not data or not data.get("found"):
        return []
    return [
        {"type": "maltego.IPv4Address", "value": ip, "weight": 90}
        for ip in data.get("resolved_ips", [])
    ]


def transform_domain_to_history(value: str) -> list[dict]:
    data = _api(f"/enrich/domain/{value}/wayback")
    if not data or not data.get("found"):
        return []
    entities = []
    first = data.get("first_archived", "")
    last  = data.get("last_archived", "")
    if first:
        entities.append({
            "type": "maltego.Domain", "value": value, "weight": 70,
            "fields": [
                {"name": "wayback_first", "display": "First Archived", "value": first},
                {"name": "wayback_last",  "display": "Last Archived",  "value": last},
                {"name": "wayback_count", "display": "Snapshots",
                 "value": str(data.get("snapshot_count", "?"))},
            ],
        })
    for path in data.get("interesting_paths", [])[:5]:
        entities.append({"type": "maltego.URL", "value": path, "weight": 60})
    return entities


def transform_ip_to_shodan(value: str) -> list[dict]:
    data = _api(f"/enrich/ip/{value}")
    if not data or not data.get("found"):
        return []
    entities = []
    if data.get("org"):
        entities.append({"type": "maltego.Organization", "value": data["org"], "weight": 80})
    country = data.get("country", "")
    city    = data.get("city", "")
    if city and country:
        entities.append({"type": "maltego.Location",
                          "value": f"{city}, {country}", "weight": 70})
    for port in data.get("ports", [])[:10]:
        entities.append({"type": "maltego.Port", "value": str(port), "weight": 50})
    return entities


# ── Dispatch ──────────────────────────────────────────────────────────────────

_TRANSFORMS = {
    "PersonToConnections": transform_person_to_connections,
    "PersonToAleph":       transform_person_to_aleph,
    "DomainToIPs":         transform_domain_to_ips,
    "DomainToHistory":     transform_domain_to_history,
    "IPToShodan":          transform_ip_to_shodan,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _TRANSFORMS:
        available = ", ".join(_TRANSFORMS)
        sys.exit(f"Usage: maltego_transforms.py <transform>\nAvailable: {available}")

    transform_name = sys.argv[1]
    xml_input = sys.stdin.read()

    _entity_type, value = _parse_request(xml_input)
    if not value:
        print(_build_response([]))
        return

    fn = _TRANSFORMS[transform_name]
    entities = fn(value)
    print(_build_response(entities))


if __name__ == "__main__":
    main()

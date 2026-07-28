"""Legacy Fieldwork API — type detection (pure-local) + graceful crawler degradation."""
import pytest

from conftest import LEGACY_API, get_json


# ── Target type auto-detection (deterministic, no external calls) ─────────────

@pytest.mark.parametrize("target,expected", [
    ("tesla.com",        "domain"),
    ("8.8.8.8",          "ip"),
    ("john@acme.com",    "email"),
    ("0x1234567890abcdef1234567890abcdef12345678", "crypto_eth"),
    ("+1 (415) 555-0132", "phone"),
    ("+447911123456",     "phone"),
])
def test_investigate_detect(client, target, expected):
    d = get_json(client, LEGACY_API, "/investigate/detect", params={"target": target})
    assert d["type"] == expected, f"{target} → {d['type']} (want {expected})"


def test_investigate_detect_rejects_overlong(client):
    r = client.get(LEGACY_API + "/investigate/detect", params={"target": "x" * 400})
    assert r.status_code == 400


# ── Crawler graceful degradation (a crawler must never 500 on bad input) ──────

def test_ipinfo_private_ip_is_graceful(client):
    # Pure-local guard: private IPs are skipped with found=false, not an error.
    d = get_json(client, LEGACY_API, "/enrich/ip/10.0.0.1/ipinfo")
    assert d["found"] is False
    assert "reason" in d


@pytest.mark.network
def test_ipinfo_public_ip(client):
    r = client.get(LEGACY_API + "/enrich/ip/8.8.8.8/ipinfo")
    assert r.status_code == 200
    d = r.json()
    if not d.get("found"):
        pytest.skip(f"ipinfo upstream unavailable: {d.get('reason')}")
    assert d.get("country") or d.get("org")

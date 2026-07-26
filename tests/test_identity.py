"""Identity Forge — synthetic cover-persona generator + persistence."""
from conftest import SHELL_API, get_json


def test_locales(client):
    d = get_json(client, SHELL_API, "/api/identity/locales")
    assert len(d["locales"]) >= 3
    assert all("code" in l and "label" in l for l in d["locales"])


def test_generate_shape(client):
    r = client.post(SHELL_API + "/api/identity/generate", json={"locale": "US", "gender": "f"}, timeout=90)
    assert r.status_code == 200, r.text[:200]
    p = r.json()
    for k in ("id", "full_name", "age", "usernames", "email_suggestions", "interests",
              "backstory", "avatar_svg", "disclaimer"):
        assert k in p, f"missing {k}"
    assert p["usernames"] and p["email_suggestions"]
    assert p["avatar_svg"].startswith("<svg")
    # guardrail: no government-ID / financial fields
    assert not ({"ssn", "passport", "credit_card", "card_number"} & set(p)), "must not generate sensitive IDs"


def test_persona_save_list_delete(client):
    p = client.post(SHELL_API + "/api/identity/generate", json={"locale": "GB"}, timeout=90).json()
    assert client.post(SHELL_API + "/api/identity/personas", json={"persona": p}).json()["saved"] is True
    ids = [x["id"] for x in get_json(client, SHELL_API, "/api/identity/personas")["personas"]]
    assert p["id"] in ids
    assert client.delete(SHELL_API + "/api/identity/personas/" + p["id"]).json()["deleted"] is True
    ids2 = [x["id"] for x in get_json(client, SHELL_API, "/api/identity/personas")["personas"]]
    assert p["id"] not in ids2

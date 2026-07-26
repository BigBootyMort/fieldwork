"""
Identity Forge — synthetic OSINT cover-persona generator (sock puppets).

Generates entirely FICTIONAL research identities so an analyst can operate burner
accounts without exposing themselves. Guardrails:
  * personas are synthetic — the backstory prompt forbids resembling real people;
  * NO government IDs (SSN/passport), NO financial numbers — deliberately not generated;
  * avatars are locally generated abstract graphics, never real faces.

Backstories use the shared Claude bridge (falls back to a template offline).
Saved personas persist as :Persona nodes in the shared Neo4j.
"""
from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from llm_bridge import claude_complete, NoClaudeError

# ── Name / place pools (small, curated; romanized) ───────────────────────────
LOCALES: dict = {
    "US": {"label": "United States", "tld": "com",
           "m": ["James", "Michael", "David", "Chris", "Daniel", "Ryan", "Kevin", "Brandon", "Tyler", "Jason"],
           "f": ["Emily", "Jessica", "Ashley", "Sarah", "Megan", "Rachel", "Lauren", "Nicole", "Amanda", "Hannah"],
           "l": ["Smith", "Johnson", "Miller", "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Brooks", "Reed"],
           "cities": ["Austin, TX", "Denver, CO", "Portland, OR", "Columbus, OH", "Raleigh, NC"]},
    "GB": {"label": "United Kingdom", "tld": "co.uk",
           "m": ["Oliver", "Jack", "Harry", "George", "Thomas", "Callum", "Liam", "Aaron", "Ben", "Sam"],
           "f": ["Amelia", "Olivia", "Emily", "Sophie", "Grace", "Chloe", "Katie", "Lucy", "Ellie", "Hannah"],
           "l": ["Taylor", "Brown", "Wilson", "Evans", "Roberts", "Walker", "Wright", "Hughes", "Green", "Hall"],
           "cities": ["Manchester", "Leeds", "Bristol", "Glasgow", "Sheffield"]},
    "DE": {"label": "Germany", "tld": "de",
           "m": ["Lukas", "Jonas", "Leon", "Felix", "Max", "Paul", "Tim", "Niklas", "Jan", "Tobias"],
           "f": ["Anna", "Lena", "Laura", "Julia", "Sarah", "Lea", "Marie", "Nina", "Katrin", "Sophie"],
           "l": ["Muller", "Schmidt", "Schneider", "Fischer", "Weber", "Wagner", "Becker", "Hoffmann", "Koch", "Bauer"],
           "cities": ["Leipzig", "Dresden", "Hannover", "Nurnberg", "Bremen"]},
    "FR": {"label": "France", "tld": "fr",
           "m": ["Lucas", "Hugo", "Nathan", "Louis", "Theo", "Antoine", "Julien", "Maxime", "Clement", "Romain"],
           "f": ["Emma", "Lea", "Manon", "Chloe", "Camille", "Sarah", "Julie", "Marie", "Laura", "Pauline"],
           "l": ["Martin", "Bernard", "Dubois", "Robert", "Moreau", "Laurent", "Simon", "Michel", "Girard", "Roux"],
           "cities": ["Lyon", "Toulouse", "Nantes", "Lille", "Bordeaux"]},
    "ES": {"label": "Spain", "tld": "es",
           "m": ["Alejandro", "Daniel", "Pablo", "Javier", "Sergio", "Adrian", "Diego", "Carlos", "Marcos", "Ivan"],
           "f": ["Lucia", "Maria", "Paula", "Sara", "Carla", "Marta", "Ana", "Elena", "Laura", "Sofia"],
           "l": ["Garcia", "Fernandez", "Lopez", "Martinez", "Sanchez", "Perez", "Gomez", "Ruiz", "Torres", "Ramos"],
           "cities": ["Valencia", "Sevilla", "Zaragoza", "Malaga", "Bilbao"]},
    "BR": {"label": "Brazil", "tld": "com.br",
           "m": ["Lucas", "Gabriel", "Mateus", "Pedro", "Rafael", "Bruno", "Felipe", "Thiago", "Gustavo", "Andre"],
           "f": ["Ana", "Julia", "Beatriz", "Mariana", "Larissa", "Camila", "Leticia", "Amanda", "Fernanda", "Bruna"],
           "l": ["Silva", "Santos", "Oliveira", "Souza", "Lima", "Costa", "Pereira", "Almeida", "Ferreira", "Rocha"],
           "cities": ["Curitiba", "Porto Alegre", "Recife", "Fortaleza", "Belo Horizonte"]},
}
OCCUPATIONS = ["graphic designer", "logistics coordinator", "freelance photographer", "data analyst",
               "nurse", "software tester", "supply-chain planner", "content writer", "sales rep",
               "HVAC technician", "junior accountant", "translator", "warehouse supervisor",
               "recruiter", "UX researcher", "civil engineer", "barista", "video editor"]
INTERESTS = ["cycling", "PC gaming", "birdwatching", "home brewing", "urban photography", "hiking",
             "vinyl records", "cooking", "amateur astronomy", "mechanical keyboards", "climbing",
             "chess", "kayaking", "3D printing", "football", "true-crime podcasts", "gardening",
             "retro gaming", "street art", "board games", "running", "aquariums"]


def _rng(seed: str | None) -> random.Random:
    return random.Random(seed) if seed else random.Random()


def _handles(first: str, last: str, r: random.Random) -> list[str]:
    f, l = first.lower(), last.lower()
    n = r.randint(2, 99)
    pool = [f"{f}.{l}", f"{f}{l}", f"{f}_{l}", f"{f[0]}{l}{n}", f"{f}{l}{n}",
            f"{f}_{l}{r.randint(1,9)}{r.randint(0,9)}", f"{l}.{f}", f"the{f}{l}"]
    r.shuffle(pool)
    return pool[:5]


def _email_locals(first: str, last: str, r: random.Random) -> list[str]:
    f, l = first.lower(), last.lower()
    return list(dict.fromkeys([f"{f}.{l}", f"{f}{l}{r.randint(10,99)}", f"{f[0]}{l}"]))[:3]


PALETTE = ["#18e0ff", "#ff2e97", "#c6ff2e", "#00ff9c", "#a86cff", "#fce94f", "#ff8a3c"]


def _avatar_svg(seed: str, initials: str) -> str:
    """Deterministic abstract avatar (never a real face) from a seed."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    c1 = PALETTE[h % len(PALETTE)]
    c2 = PALETTE[(h // 13) % len(PALETTE)]
    # a few seeded geometric ticks for identicon flavour
    ticks = ""
    for i in range(6):
        if (h >> i) & 1:
            x = 12 + i * 13
            ticks += f'<rect x="{x}" y="6" width="7" height="7" fill="{c2}" opacity="0.7"/>'
    gid = "g" + hashlib.md5(seed.encode()).hexdigest()[:6]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
        f'<rect width="100" height="100" fill="#0a0c16"/>'
        f'<circle cx="50" cy="52" r="30" fill="url(#{gid})" opacity="0.9"/>'
        f'<circle cx="50" cy="52" r="30" fill="none" stroke="{c1}" stroke-opacity="0.6"/>'
        f'{ticks}'
        f'<text x="50" y="62" font-family="\'JetBrains Mono\',monospace" font-size="26" font-weight="700" '
        f'fill="#04050a" text-anchor="middle">{initials}</text></svg>'
    )


async def _backstory(deps, p: dict) -> tuple[str, str]:
    system = (
        "You write concise COVER PERSONAS for authorized OSINT research. The person is "
        "entirely FICTIONAL — never reference, name, or resemble a real identifiable person, "
        "and never invent sensitive identifiers (IDs, financial data). Write a plausible "
        "3-4 sentence third-person backstory: origin, work, a hobby, and one believable online "
        "habit that makes the persona credible. Neutral, unremarkable, forgettable. "
        "Output ONLY the backstory prose — no heading, no markdown, no preamble."
    )
    user = (f"{p['full_name']}, {p['age']}, {p['gender']}, {p['occupation']} in {p['city']}, "
            f"{p['country']}. Interests: {', '.join(p['interests'])}.")
    try:
        text, engine = await claude_complete(system=system, user=user, http=deps["http"], max_tokens=350)
        return text.strip(), engine
    except NoClaudeError:
        return (f"{p['full_name']} is a {p['age']}-year-old {p['occupation']} based in {p['city']}, "
                f"{p['country']}. Keeps a low profile online, mostly lurking in communities around "
                f"{p['interests'][0]} and {p['interests'][1]}. Unremarkable footprint — the point."), "template"
    except Exception:
        return "(backstory unavailable)", "none"


class GenReq(BaseModel):
    locale: str | None = None
    gender: str | None = None            # "m" | "f" | None (random)
    age_min: int = 22
    age_max: int = 48
    seed: str | None = None


class SaveReq(BaseModel):
    persona: dict
    label: str | None = None


def build_router(deps: dict) -> APIRouter:
    router = APIRouter()
    graph = deps["graph_db"]
    audit = deps["audit"]

    def _generate(req: GenReq) -> dict:
        r = _rng(req.seed)
        loc_key = req.locale if req.locale in LOCALES else r.choice(list(LOCALES))
        loc = LOCALES[loc_key]
        gender = req.gender if req.gender in ("m", "f") else r.choice(["m", "f"])
        first = r.choice(loc[gender])
        last = r.choice(loc["l"])
        age = r.randint(max(18, req.age_min), max(req.age_max, req.age_min + 1))
        yob = datetime.now(timezone.utc).year - age
        dob = f"{yob}-{r.randint(1,12):02d}-{r.randint(1,28):02d}"
        interests = r.sample(INTERESTS, 3)
        handles = _handles(first, last, r)
        email_locals = _email_locals(first, last, r)
        return {
            "id": uuid.uuid4().hex[:12],
            "first_name": first, "last_name": last, "full_name": f"{first} {last}",
            "gender": "male" if gender == "m" else "female",
            "age": age, "dob": dob,
            "locale": loc_key, "country": loc["label"], "city": r.choice(loc["cities"]),
            "occupation": r.choice(OCCUPATIONS), "interests": interests,
            "usernames": handles,
            "email_suggestions": [f"{h}@{prov}" for h, prov in
                                  zip(email_locals, ["gmail.com", "outlook.com", f"mail.{loc['tld']}"])],
            "avatar_svg": _avatar_svg(first + last + str(age), (first[0] + last[0]).upper()),
            "disclaimer": "Synthetic persona for authorized OSINT research only — not a real person.",
        }

    @router.get("/locales")
    async def locales():
        return {"locales": [{"code": k, "label": v["label"]} for k, v in LOCALES.items()]}

    @router.post("/generate")
    async def generate(req: GenReq):
        p = _generate(req)
        p["backstory"], p["backstory_engine"] = await _backstory(deps, p)
        audit(action="IdentityGenerate", subject=p["full_name"],
              detail=f"{p['locale']} {p['age']} engine={p['backstory_engine']}")
        return p

    @router.get("/personas")
    async def list_personas():
        async with graph.session() as s:
            res = await s.run("MATCH (p:Persona) RETURN p ORDER BY p.saved_at DESC LIMIT 200")
            out = []
            async for rec in res:
                out.append(dict(rec["p"]))
            return {"personas": out}

    @router.post("/personas")
    async def save_persona(req: SaveReq):
        p = dict(req.persona or {})
        if not p.get("id") or not p.get("full_name"):
            raise HTTPException(400, "persona with id and full_name required")
        p["label"] = req.label or ""
        # Neo4j stores scalars + string arrays; keep it flat.
        props = {k: v for k, v in p.items() if isinstance(v, (str, int, float, bool))}
        props["usernames"] = [str(x) for x in p.get("usernames", [])]
        props["email_suggestions"] = [str(x) for x in p.get("email_suggestions", [])]
        props["interests"] = [str(x) for x in p.get("interests", [])]
        async with graph.session() as s:
            await s.run(
                "MERGE (p:Persona {id:$id}) SET p += $props, p.saved_at = datetime()",
                id=p["id"], props=props,
            )
        audit(action="IdentitySave", subject=p["full_name"], detail=p["id"])
        return {"saved": True, "id": p["id"]}

    @router.delete("/personas/{pid}")
    async def delete_persona(pid: str):
        async with graph.session() as s:
            await s.run("MATCH (p:Persona {id:$id}) DETACH DELETE p", id=pid)
        audit(action="IdentityDelete", subject=pid)
        return {"deleted": True}

    return router

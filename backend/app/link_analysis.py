"""
Claude link analysis over the investigation knowledge graph.

Two structural passes, then Claude reasoning:
  1. SHARED ENTITIES  — nodes connected to >= 2 investigated targets
                        ("these targets share an IP / email / org / wallet").
  2. DUPLICATE CANDIDATES — same-label nodes whose names overlap
                        (entity-resolution suggestions to merge).

The structural findings are fed to Claude (subscription bridge → Ollama) to
produce a ranked narrative: notable hidden links, suggested merges, and the
highest-value next pivots. Returns both the raw structure and the analysis.
"""
from __future__ import annotations

import logging

import httpx

from llm_bridge import claude_complete, NoClaudeError
from llm import OLLAMA_URL, OLLAMA_MODEL

log = logging.getLogger("fieldwork.link_analysis")

_SYSTEM = """\
You are an OSINT link-analysis specialist. From the graph findings provided,
produce a concise, ranked analysis. Use these markdown sections:

## Hidden connections
The most significant entities shared between multiple targets, ranked by
investigative value. Explain WHY each link matters in one line. Cite the shared
entity and the targets it links.

## Likely duplicate entities
Pairs that probably refer to the same real-world entity and should be merged.
State your confidence (high/medium/low) and the reasoning.

## Top pivots
3-5 specific next moves: which entity to investigate next and what it could reveal.

Be specific and terse. If a section has nothing, write "None found."
"""


async def _shared_entities(graph_db, limit: int = 40) -> list[dict]:
    async with graph_db.driver.session() as s:
        r = await s.run(
            """
            MATCH (i:Investigation)-[:INVESTIGATED]->(t)
            MATCH (t)--(shared)
            WHERE NOT shared:Investigation
            WITH shared, collect(DISTINCT coalesce(t.name, t.id)) AS targets
            WHERE size(targets) >= 2
            RETURN labels(shared) AS labels,
                   coalesce(shared.name, shared.id) AS name,
                   targets, size(targets) AS n
            ORDER BY n DESC
            LIMIT $limit
            """, limit=limit,
        )
        out = []
        for rec in await r.fetch(limit):
            labs = [l for l in (rec["labels"] or []) if l != "Entity"]
            out.append({"label": (labs or ["Entity"])[0], "name": rec["name"],
                        "shared_by": rec["targets"], "count": rec["n"]})
        return out


async def _duplicate_candidates(graph_db, limit: int = 30) -> list[dict]:
    async with graph_db.driver.session() as s:
        r = await s.run(
            """
            MATCH (a), (b)
            WHERE id(a) < id(b)
              AND any(l IN labels(a) WHERE l IN ['Person','Company','Organization','Email'])
              AND labels(a) = labels(b)
              AND a.name IS NOT NULL AND b.name IS NOT NULL
              AND a.name <> b.name
              AND ( toLower(a.name) CONTAINS toLower(b.name)
                 OR toLower(b.name) CONTAINS toLower(a.name) )
            RETURN labels(a) AS labels, a.name AS a_name, b.name AS b_name,
                   a.id AS a_id, b.id AS b_id
            LIMIT $limit
            """, limit=limit,
        )
        out = []
        for rec in await r.fetch(limit):
            labs = [l for l in (rec["labels"] or []) if l != "Entity"]
            out.append({"label": (labs or ["Entity"])[0],
                        "a": rec["a_name"], "b": rec["b_name"],
                        "a_id": rec["a_id"], "b_id": rec["b_id"]})
        return out


def _digest(shared: list[dict], dupes: list[dict]) -> str:
    parts = ["SHARED ENTITIES (connected to multiple investigated targets):"]
    if shared:
        for s in shared[:30]:
            parts.append(f"- [{s['label']}] {s['name']} — shared by: {', '.join(s['shared_by'])}")
    else:
        parts.append("- none")
    parts.append("\nDUPLICATE CANDIDATES (same label, overlapping names):")
    if dupes:
        for d in dupes[:25]:
            parts.append(f"- [{d['label']}] \"{d['a']}\"  ≈  \"{d['b']}\"")
    else:
        parts.append("- none")
    return "\n".join(parts)


async def analyze(graph_db) -> dict:
    """Run structural passes + Claude synthesis over the whole graph."""
    if not graph_db or not getattr(graph_db, "driver", None):
        return {"error": "graph unavailable"}

    shared = await _shared_entities(graph_db)
    dupes  = await _duplicate_candidates(graph_db)

    if not shared and not dupes:
        return {"shared_entities": [], "duplicates": [], "engine": None,
                "analysis": "_No cross-entity links or duplicates yet — run a few "
                            "investigations first (each adds to the graph)._"}

    digest = _digest(shared, dupes)
    analysis, engine = "", None
    async with httpx.AsyncClient(timeout=200) as client:
        try:
            analysis, engine = await claude_complete(
                system=_SYSTEM, user=digest, http=client, max_tokens=1500,
            )
        except NoClaudeError:
            try:
                r = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": OLLAMA_MODEL, "system": _SYSTEM,
                          "prompt": digest, "stream": False},
                )
                r.raise_for_status()
                analysis, engine = r.json().get("response", "").strip(), "ollama"
            except Exception as exc:
                analysis = f"_Synthesis unavailable: {exc} — structural findings shown above._"
        except Exception as exc:
            analysis = f"_Analysis failed: {exc}_"

    return {
        "shared_entities": shared,
        "duplicates":      dupes,
        "analysis":        analysis,
        "engine":          engine,
    }

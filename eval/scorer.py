"""
Runi-OS investigation eval — scoring primitives.

Pure standard-library, deterministic, no network and no LLM calls. These functions
score a synthesized intelligence *brief* against the *evidence* that was actually
collected, so we can measure the synthesis layer the way you'd measure any RAG system:

  - grounding      : of the concrete entities the brief asserts, what fraction are
                     actually present in the collected evidence? (faithfulness / precision)
  - hallucinations : how many planted "trap" facts — things that never appear in the
                     evidence — leaked into the brief?
  - consistency    : across N runs on the same target, how stable is the set of entities
                     the model surfaces? (mean pairwise Jaccard)

The point is a number you can defend in an interview, not a vibe. Entity extraction is
intentionally simple and transparent (regex over emails / domains / IPs / ETH addresses /
proper-noun phrases) so the metric is explainable and reproducible.
"""
from __future__ import annotations

import re
from itertools import combinations

# ── entity extractors ────────────────────────────────────────────────────────
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ETH = re.compile(r"0x[a-fA-F0-9]{40}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.I)
# 2+ consecutive Capitalized tokens → a name / org ("Acme Robotics", "Jane Doe").
_PROPER = re.compile(r"\b([A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)+)\b")

# words that look capitalized because they start a sentence / are headings, not names.
_STOP = {
    "The", "This", "That", "There", "These", "Those", "It", "He", "She", "They",
    "A", "An", "In", "On", "At", "No", "Not", "None", "Based", "According",
    "Coverage", "Summary", "Findings", "Brief", "Confidence", "Note", "Sources",
    "Overview", "Assessment", "Unverified", "Weak", "Data",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def extract_entities(text: str) -> set[str]:
    """Return a normalized set of concrete entities mentioned in *text*."""
    if not text:
        return set()
    ents: set[str] = set()
    ents |= {m.group(0).lower() for m in _EMAIL.finditer(text)}
    ents |= {m.group(0).lower() for m in _ETH.finditer(text)}
    ents |= {m.group(0) for m in _IPV4.finditer(text)}
    # domains, but not the local part of an already-captured email
    for m in _DOMAIN.finditer(text):
        d = m.group(0).lower()
        if "@" not in d and "." in d and not d.replace(".", "").isdigit():
            ents.add(d)
    for m in _PROPER.finditer(text):
        phrase = m.group(1)
        first = phrase.split()[0]
        if first not in _STOP:
            ents.add(_norm(phrase))
    return ents


def _grounded(entity: str, evidence: str) -> bool:
    """Is *entity* actually supported by the evidence text?"""
    ev = evidence.lower()
    if entity in ev:
        return True
    # multiword proper nouns: grounded if every token appears in the evidence
    toks = entity.split()
    if len(toks) > 1:
        return all(t in ev for t in toks)
    return False


# ── metrics ──────────────────────────────────────────────────────────────────
def grounding_score(brief: str, evidence: str) -> dict:
    """Fraction of the brief's entities that are supported by the evidence.

    Returns {score, grounded, total, ungrounded:[...]}. An empty brief scores 1.0
    (it asserts nothing false) but is flagged via total=0 so it can't hide.
    """
    ents = extract_entities(brief)
    if not ents:
        return {"score": 1.0, "grounded": 0, "total": 0, "ungrounded": []}
    ungrounded = sorted(e for e in ents if not _grounded(e, evidence))
    grounded = len(ents) - len(ungrounded)
    return {
        "score": round(grounded / len(ents), 4),
        "grounded": grounded,
        "total": len(ents),
        "ungrounded": ungrounded,
    }


def hallucination_hits(brief: str, traps: list[str]) -> dict:
    """Count planted trap facts (never in the evidence) that leaked into the brief."""
    b = brief.lower()
    hits = sorted({t for t in traps if t and t.lower() in b})
    return {"count": len(hits), "hits": hits}


def consistency_score(briefs: list[str]) -> dict:
    """Mean pairwise Jaccard similarity of the entity sets across repeated runs."""
    ent_sets = [extract_entities(b) for b in briefs if b]
    if len(ent_sets) < 2:
        return {"score": None, "runs": len(ent_sets), "note": "need >= 2 runs"}
    sims = []
    for a, b in combinations(ent_sets, 2):
        union = a | b
        sims.append(len(a & b) / len(union) if union else 1.0)
    return {"score": round(sum(sims) / len(sims), 4), "runs": len(ent_sets)}


def score_case(brief: str, evidence: str, traps: list[str]) -> dict:
    """Convenience: all single-brief metrics for one case."""
    g = grounding_score(brief, evidence)
    h = hallucination_hits(brief, traps)
    return {
        "grounding": g["score"],
        "grounded_entities": f"{g['grounded']}/{g['total']}",
        "ungrounded": g["ungrounded"],
        "hallucinations": h["count"],
        "hallucinated_facts": h["hits"],
    }

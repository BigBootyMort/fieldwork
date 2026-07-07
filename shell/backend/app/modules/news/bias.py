"""
Media bias / lean / ownership annotations for news sources.

Ratings are an approximation of third-party consensus (AllSides / Media
Bias-Fact-Check style) — political lean, factual reliability, and who owns or
funds the outlet (incl. government influence). They are advisory, not absolute:
outlets shift over time and individual stories vary. The point is to surface
*whose* perspective a story comes from and how balanced the coverage is.

lean_score: -10 (far left) … 0 (center) … +10 (far right)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_L, _CL, _C, _CR, _R = -7, -3, 0, 3, 7   # lean → numeric score

# Matched as a lowercase substring against the outlet / source name.
# Order matters only for readability; matching picks the longest hit.
SOURCE_BIAS: dict[str, dict] = {
    # ── Wire / agencies (generally center) ─────────────────────────────
    "associated press": {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "US · non-profit cooperative"},
    "reuters":          {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "US/UK · Thomson Reuters"},
    "afp":              {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "France · agency (partly state-linked)"},
    "npr":              {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "US · public radio"},
    "bloomberg":        {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "US · Bloomberg LP"},
    # ── Papers of record ───────────────────────────────────────────────
    "bbc":              {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "UK · public broadcaster (licence-funded)"},
    "new york times":   {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "US · private (NYT Co.)"},
    "the guardian":     {"lean": "Left",         "score": _L,  "factual": "High",     "owner": "UK · Scott Trust"},
    "washington post":  {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "US · Nash Holdings (J. Bezos)"},
    "wall street journal": {"lean": "Center-Right","score": _CR,"factual": "High",     "owner": "US · News Corp (Murdoch)"},
    "the economist":    {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "UK · private"},
    "financial times":  {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "UK · Nikkei (Japan)"},
    # ── US cable / partisan ────────────────────────────────────────────
    "fox news":         {"lean": "Right",        "score": _R,  "factual": "Mixed",    "owner": "US · Fox Corp (Murdoch)"},
    "cnn":              {"lean": "Center-Left",  "score": _CL, "factual": "Mixed",    "owner": "US · Warner Bros. Discovery"},
    "msnbc":            {"lean": "Left",         "score": _L,  "factual": "Mixed",    "owner": "US · NBCUniversal"},
    "breitbart":        {"lean": "Far Right",    "score": 9,   "factual": "Low",      "owner": "US · private (conservative)"},
    "the daily wire":   {"lean": "Right",        "score": _R,  "factual": "Mixed",    "owner": "US · private (conservative)"},
    "huffpost":         {"lean": "Left",         "score": _L,  "factual": "Mixed",    "owner": "US · BuzzFeed"},
    "the hill":         {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "US · Nexstar"},
    "politico":         {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "US · Axel Springer (Germany)"},
    # ── State-funded / government-influenced (the key 'influence' cases) ─
    "al jazeera":       {"lean": "Center-Left",  "score": _CL, "factual": "Mixed",    "owner": "⚑ State-funded · Qatar govt"},
    "deutsche welle":   {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "⚑ State-funded · Germany (public)"},
    "france 24":        {"lean": "Center",       "score": _C,  "factual": "High",     "owner": "⚑ State-funded · France (public)"},
    "rt":               {"lean": "Right",        "score": _R,  "factual": "Low",      "owner": "⚑ State media · Russia govt"},
    "russia today":     {"lean": "Right",        "score": _R,  "factual": "Low",      "owner": "⚑ State media · Russia govt"},
    "sputnik":          {"lean": "Right",        "score": _R,  "factual": "Low",      "owner": "⚑ State media · Russia govt"},
    "cgtn":             {"lean": "Left",         "score": _L,  "factual": "Low",      "owner": "⚑ State media · China govt"},
    "xinhua":           {"lean": "Left",         "score": _L,  "factual": "Low",      "owner": "⚑ State media · China govt"},
    "global times":     {"lean": "Left",         "score": _L,  "factual": "Low",      "owner": "⚑ State media · China (CCP)"},
    "press tv":         {"lean": "Left",         "score": _L,  "factual": "Low",      "owner": "⚑ State media · Iran govt"},
    "tass":             {"lean": "Center",       "score": _C,  "factual": "Low",      "owner": "⚑ State agency · Russia govt"},
    # ── Tech / security (political lean not really applicable) ──────────
    "ars technica":     {"lean": "Center-Left",  "score": _CL, "factual": "High",     "owner": "US · Condé Nast · tech"},
    "hacker news":      {"lean": "N/A",          "score": _C,  "factual": "Aggregator","owner": "US · Y Combinator · tech aggregator"},
    "krebs":            {"lean": "N/A",          "score": _C,  "factual": "High",     "owner": "US · independent · security"},
    "bleepingcomputer": {"lean": "N/A",          "score": _C,  "factual": "High",     "owner": "US · independent · security"},
    "the record":       {"lean": "N/A",          "score": _C,  "factual": "High",     "owner": "US · Recorded Future · security"},
    # ── Aggregators ────────────────────────────────────────────────────
    "google news":      {"lean": "Mixed",        "score": _C,  "factual": "Aggregator","owner": "US · aggregator (many outlets)"},
}

_UNRATED = {"lean": "Unrated", "score": None, "factual": "Unrated", "owner": "—"}

# ── AI-estimated lean cache (fills gaps for outlets not in the curated list) ─
# Persisted so estimates are stable/consistent (not re-rolled per request) and
# clearly marked ai_estimated=True so they are never confused with the curated
# consensus ratings above.
_AI_CACHE_PATH = Path(__file__).parent / "bias_ai_cache.json"
_ai_cache: dict | None = None


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _load_ai_cache() -> dict:
    global _ai_cache
    if _ai_cache is None:
        try:
            _ai_cache = json.loads(_AI_CACHE_PATH.read_text(encoding="utf-8")) \
                if _AI_CACHE_PATH.exists() else {}
        except Exception:
            _ai_cache = {}
    return _ai_cache


def ai_lookup(outlet: str) -> dict | None:
    """Return a cached AI estimate for an outlet (only medium/high confidence)."""
    c = _load_ai_cache()
    hit = c.get(_norm(outlet))
    if hit and hit.get("confidence") in ("medium", "high"):
        return hit
    return None


def record_ai_estimates(estimates: list[dict]) -> None:
    """Persist a batch of AI estimates. Low-confidence rows are stored too so we
    don't keep re-asking, but ai_lookup() will still treat them as Unrated."""
    c = _load_ai_cache()
    for e in estimates or []:
        outlet = e.get("outlet")
        if not outlet:
            continue
        c[_norm(outlet)] = {
            "lean":    e.get("lean", "Unrated"),
            "score":   e.get("score"),
            "factual": e.get("factual", "Unrated"),
            "owner":   e.get("owner", "—"),
            "confidence": (e.get("confidence") or "low").lower(),
            "ai_estimated": True,
        }
    try:
        _AI_CACHE_PATH.write_text(json.dumps(c, indent=2), encoding="utf-8")
    except Exception:
        pass

# Google-News-style titles end with "  Headline - Outlet Name"
_TITLE_OUTLET = re.compile(r"\s[-–—]\s([A-Z][\w&.'’ ]{2,40})$")


def _match(text: str) -> dict | None:
    t = (text or "").lower()
    best, best_len = None, 0
    for token, data in SOURCE_BIAS.items():
        if token in t and len(token) > best_len:
            best, best_len = data, len(token)
    return best


def bias_for(source_name: str, title: str = "") -> dict:
    """
    Resolve the media-bias annotation for an article. Tries the underlying
    outlet parsed from a Google-News-style title suffix first, then the RSS
    source name. Returns a dict with lean/score/factual/owner (+ 'outlet').
    """
    outlet = ""
    if title:
        m = _TITLE_OUTLET.search(title.strip())
        if m:
            outlet = m.group(1).strip()
            hit = _match(outlet)
            if hit:
                return {**hit, "outlet": outlet}
    hit = _match(source_name)
    if hit:
        return {**hit, "outlet": outlet or source_name}
    # Fall back to an AI estimate if one is cached for this outlet.
    name = outlet or source_name
    ai = ai_lookup(name)
    if ai:
        return {**ai, "outlet": name}
    return {**_UNRATED, "outlet": name}


def outlet_of(source_name: str, title: str = "") -> str:
    """The best guess at the underlying outlet name (for gap-filling)."""
    if title:
        m = _TITLE_OUTLET.search(title.strip())
        if m:
            return m.group(1).strip()
    return source_name


def is_rated(source_name: str, title: str = "") -> bool:
    """True if the outlet has a curated OR cached-AI rating (medium/high)."""
    name = outlet_of(source_name, title)
    return _match(name) is not None or _match(source_name) is not None \
        or ai_lookup(name) is not None


# ── Neutral AI lean estimation (for outlets missing from the curated list) ──

# The neutrality of this feature lives almost entirely in this prompt: rate the
# OUTLET (not articles), anchor to fixed reference points, force symmetry, cite
# third-party-rater consensus rather than the model's own view, and refuse to
# guess when unsure.
ESTIMATE_SYSTEM = """\
You are a neutral media-reference tool. Report the GENERALLY DOCUMENTED editorial
lean of news outlets — as assessed by established bias raters (AllSides, Media
Bias/Fact Check, Ad Fontes) and by ownership/funding facts — NOT your own opinion.

Neutrality rules (follow strictly):
- Rate the OUTLET's typical stance, never a single article.
- Base it only on verifiable factors: ownership, funding (incl. government), and
  the consensus of third-party bias raters.
- Anchor EVERY rating to this fixed scale so you don't drift:
    Associated Press / Reuters = Center (0)
    New York Times / NPR       = Center-Left (-3)
    The Guardian / MSNBC       = Left (-7)
    Wall Street Journal (news) = Center-Right (+3)
    Fox News                   = Right (+7)
- Treat left and right SYMMETRICALLY. Do not systematically nudge outlets toward
  either side. Reuse the anchors' scores for comparable outlets.
- If you do not recognise the outlet or are not reasonably confident, set
  lean="Unrated" and confidence="low". DO NOT guess a lean to seem helpful.
- Note government/state funding explicitly in "owner".

Output ONLY a JSON array, one object per outlet:
[{"outlet","lean","score","factual","owner","confidence"}]
lean ∈ {Left, Center-Left, Center, Center-Right, Right, Unrated}
score ∈ integer [-8,8]   factual ∈ {High, Mixed, Low}   confidence ∈ {high, medium, low}
"""


def build_estimation_prompt(outlets: list[str]) -> str:
    lines = "\n".join(f"- {o}" for o in outlets)
    return ("Rate the general editorial lean of these news outlets, following the "
            "neutrality rules exactly:\n\n" + lines)


def parse_estimation(text: str) -> list[dict]:
    """Extract the JSON array of estimates from an LLM reply, tolerating fences."""
    if not text:
        return []
    t = re.sub(r"```(?:json)?", "", text)
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(t[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def coverage_balance(sources: list[str], titles: list[str] | None = None) -> dict:
    """
    Summarise the political spread of a set of sources (e.g. a story cluster):
    counts per lean bucket, an average lean score, a one-word balance label,
    and whether any state-funded outlet is present.
    """
    titles = titles or [""] * len(sources)
    buckets = {"left": 0, "center": 0, "right": 0, "na": 0}
    scores, state = [], False
    for s, t in zip(sources, titles):
        b = bias_for(s, t)
        sc = b.get("score")
        if "State" in (b.get("owner") or "") or "⚑" in (b.get("owner") or ""):
            state = True
        if sc is None or b.get("lean") in ("N/A", "Unrated", "Mixed"):
            buckets["na"] += 1
            continue
        scores.append(sc)
        if sc <= -2:
            buckets["left"] += 1
        elif sc >= 2:
            buckets["right"] += 1
        else:
            buckets["center"] += 1
    avg = round(sum(scores) / len(scores), 1) if scores else None
    if avg is None:
        label = "unrated"
    elif avg <= -3:
        label = "left-leaning coverage"
    elif avg <= -1:
        label = "center-left coverage"
    elif avg < 1:
        label = "balanced / center"
    elif avg < 3:
        label = "center-right coverage"
    else:
        label = "right-leaning coverage"
    return {"buckets": buckets, "avg_score": avg, "label": label,
            "state_funded_present": state}

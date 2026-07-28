"""
Relevance gate for keyword-based OSINT crawlers.

Court records, adverse-media, and reddit searches match on a name string and
routinely return items about *other* people/entities. Left unfiltered, the
synthesis LLM launders them as confirmed facts (e.g. unrelated court cases
attributed to the target). This module demotes items whose text doesn't
verifiably mention the target into a `weak_matches` bucket the brief is told to
ignore, and keeps the headline counts (found/total) honest.

Pure-stdlib (only `re`) so it is unit-testable without the crawler stack.
"""
from __future__ import annotations

import re

_NAME_STOP = {
    "the", "and", "of", "for", "inc", "llc", "ltd", "corp", "co", "plc", "gmbh",
    "group", "holding", "holdings", "company", "limited", "sa", "ag", "llp", "lp",
    "mr", "mrs", "ms", "dr", "jr", "sr", "van", "von", "de", "la", "el",
}

# tool → (list_key, text_keys used to test whether an item mentions the target)
RELEVANCE_TARGETS = {
    "Court Records":   ("cases",    ("case_name", "snippet", "court")),
    "Adverse Media":   ("articles", ("title", "snippet", "url")),
    "Reddit (search)": ("posts",    ("title", "selftext", "body", "subreddit")),
}


def name_tokens(text: str) -> list[str]:
    """Distinctive lowercase tokens of a name/entity string (stopwords dropped)."""
    return [w for w in re.findall(r"[a-z0-9]{2,}", (text or "").lower())
            if w not in _NAME_STOP]


def is_relevant(target_toks: list[str], surname: str, text_toks: set[str]) -> bool:
    """True if `text_toks` plausibly refers to the target.

    Relevant when the distinctive last token (surname) is present, or at least
    half of the target tokens (minimum 2) appear.
    """
    if not target_toks:
        return True
    if surname and surname in text_toks:
        return True
    hits = sum(1 for tok in target_toks if tok in text_toks)
    return hits >= max(2, (len(target_toks) + 1) // 2)


def apply_relevance(value: str, results: dict) -> None:
    """In-place: split each keyword crawler's hits into relevant vs weak_matches
    and honestly recompute found/total so downstream coverage/brief aren't misled.
    """
    target_toks = name_tokens(value)
    surname = target_toks[-1] if target_toks else ""
    for tool, (list_key, text_keys) in RELEVANCE_TARGETS.items():
        res = results.get(tool)
        if not isinstance(res, dict) or res.get("error"):
            continue
        items = res.get(list_key)
        if not isinstance(items, list) or not items:
            continue
        kept, weak = [], []
        for it in items:
            blob = (" ".join(str(it.get(k, "")) for k in text_keys)
                    if isinstance(it, dict) else str(it))
            bucket = kept if is_relevant(target_toks, surname, set(name_tokens(blob))) else weak
            bucket.append(it)
        if not weak:
            continue  # nothing to demote — leave the result untouched
        res[list_key] = kept
        res["weak_matches"] = weak
        res["relevance"] = {"kept": len(kept), "filtered_out": len(weak),
                            "note": "items not mentioning the target were demoted "
                                    "(low-confidence keyword matches)"}
        # Keep the headline counts honest.
        if "total" in res:
            res["total"] = len(kept)
        if "adverse_count" in res:
            res["adverse_count"] = len(kept)
        if res.get("found") is not None:
            res["found"] = bool(kept)

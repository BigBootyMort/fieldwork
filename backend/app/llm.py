"""
Fieldwork — Phase 16: Local LLM integration via Ollama.

Three capabilities:
  extract_entities(text)        → structured entity list from free-form text
  summarize_case(bundle)        → narrative investigation brief
  suggest_hypotheses(bundle)    → actionable investigative leads

All calls use the /api/generate endpoint with stream=False so the callers
receive a complete response in one shot. Timeout is 3 minutes — plenty for
a 3-8 B parameter model running on CPU.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

from llm_bridge import claude_complete, NoClaudeError  # Claude API → subscription bridge

log = logging.getLogger(__name__)

OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# ── Prompts ───────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are an OSINT entity extractor embedded in an investigative research tool.
Given text, identify every named, concrete OSINT-relevant entity.

Return ONLY a valid JSON array — no prose, no markdown fences, just the raw array.
Each element must be:
  {"type": <EntityType>, "value": <exact string as it appears>, "context": <one sentence>}

EntityType must be one of:
  Person | Organization | Email | Domain | IP | Phone | Location | CryptoAddress | Username | URL

Rules:
- Extract verbatim values — do not normalise or canonicalise
- Persons: full names only (at least first + last)
- Skip generic words, adjectives, common nouns, countries used as adjectives
- Domains: bare domain only (e.g. example.com, not https://example.com)
- URLs: include full URL with scheme
- If nothing is found return []
"""

_SUMMARIZE_SYSTEM = """\
You are a senior OSINT analyst writing an internal investigation brief for a journalist.
Write in a factual, professional tone — paragraph form, no bullet points.
Classify claims by confidence where relevant (confirmed / alleged / unverified).
Do not invent facts; only use what is provided.
"""

_HYPOTHESIS_SYSTEM = """\
You are a senior OSINT investigator generating a structured lead list.
Be specific, concrete, and actionable. Reference actual data from the case.
Use clear markdown headers and numbered lists for readability.
"""

# ── Core generate call ────────────────────────────────────────────────────────

async def _generate(
    prompt:  str,
    system:  str = "",
    model:   Optional[str] = None,
    timeout: int = 180,
) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. Claude (API → subscription bridge); 2. Ollama fallback
        try:
            text, _engine = await claude_complete(
                system=system or "You are a precise OSINT analysis assistant.",
                user=prompt, http=client, temperature=0.1, max_tokens=2048,
            )
            if text:
                return text.strip()
        except NoClaudeError:
            pass
        except Exception as exc:
            log.warning("Claude generate failed (%s) — using Ollama", exc)

        m = model or OLLAMA_MODEL
        payload = {
            "model":   m,
            "prompt":  prompt,
            "system":  system,
            "stream":  False,
            "options": {"temperature": 0.1, "num_predict": 2048},
        }
        r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return r.json()["response"].strip()


# ── JSON parsing helper ───────────────────────────────────────────────────────

def _parse_json_array(text: str) -> list:
    """Extract the first [...] block from LLM output, tolerating markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1:
        return []
    return json.loads(text[start : end + 1])


# ── Public API ────────────────────────────────────────────────────────────────

VALID_ENTITY_TYPES = frozenset({
    "Person", "Organization", "Email", "Domain", "IP",
    "Phone", "Location", "CryptoAddress", "Username", "URL",
})


async def extract_entities(text: str, model: Optional[str] = None) -> list[dict]:
    """Return a list of {type, value, context} dicts extracted from free-form text."""
    # Truncate input — 8 000 chars is ample context for a 3 B model
    response = await _generate(
        prompt=f"Extract all OSINT entities from this text:\n\n{text[:8000]}",
        system=_EXTRACT_SYSTEM,
        model=model,
    )
    try:
        raw = _parse_json_array(response)
        # Validate and sanitise each entry
        clean = []
        for item in raw:
            t = item.get("type", "")
            v = item.get("value", "")
            if t in VALID_ENTITY_TYPES and v:
                clean.append({
                    "type":    t,
                    "value":   str(v).strip(),
                    "context": str(item.get("context", "")).strip(),
                })
        return clean
    except Exception:
        log.warning("Entity extraction: JSON parse failed. Raw: %.300s", response)
        return []


def _bundle_to_text(bundle: dict, max_notes: int = 20, max_subjects: int = 15) -> str:
    """Flatten a case bundle into a structured text block for the LLM."""
    case     = bundle.get("case",     {})
    notes    = bundle.get("notes",    [])[:max_notes]
    subjects = bundle.get("subjects", [])[:max_subjects]
    tasks    = bundle.get("tasks",    [])

    subj_lines = "\n".join(
        f"  - [{s.get('entity_label','?')}] {s.get('entity_id','')} "
        f"(role: {s.get('role','')})"
        for s in subjects
    ) or "  None."

    note_lines = "\n".join(
        f"  [{n.get('type','note')}] {n.get('body','')}"
        for n in notes
    ) or "  None."

    task_lines = "\n".join(
        f"  [{'✓' if t.get('done') else '○'}] {t.get('text','')}"
        for t in tasks
    ) or "  None."

    return (
        f"CASE: {case.get('title','Untitled')}\n"
        f"Status: {case.get('status','?')}  Priority: {case.get('priority','?')}\n"
        f"Description: {case.get('description','—')}\n\n"
        f"SUBJECTS:\n{subj_lines}\n\n"
        f"NOTES:\n{note_lines}\n\n"
        f"TASKS:\n{task_lines}"
    )


async def summarize_case(bundle: dict, model: Optional[str] = None) -> str:
    """Generate a 3–5 paragraph narrative investigation summary from a case bundle."""
    context = _bundle_to_text(bundle)
    prompt = (
        f"{context}\n\n"
        "Write a 3–5 paragraph investigation summary covering:\n"
        "1. Who the subjects are and what is known about them\n"
        "2. Key findings and confirmed connections\n"
        "3. Open questions and unverified claims\n"
        "4. Recommended next steps"
    )
    return await _generate(prompt=prompt, system=_SUMMARIZE_SYSTEM, model=model)


async def suggest_hypotheses(bundle: dict, model: Optional[str] = None) -> str:
    """Generate structured investigation leads and hypotheses from a case bundle."""
    context = _bundle_to_text(bundle)
    prompt = (
        f"{context}\n\n"
        "Generate a structured investigative plan with these four sections:\n\n"
        "## Hypotheses\n"
        "List 3 testable hypotheses about what is happening or who is responsible.\n\n"
        "## Priority data points to find next\n"
        "List 5 specific, concrete pieces of information to locate (include suggested sources).\n\n"
        "## Red flags\n"
        "List any anomalies, inconsistencies, or evasion indicators in the existing data.\n\n"
        "## Entity connections worth testing\n"
        "List 3 specific entity pairs or clusters that may have hidden links worth probing."
    )
    return await _generate(prompt=prompt, system=_HYPOTHESIS_SYSTEM, model=model)


# ── Investigation Chat ────────────────────────────────────────────────────────

_CHAT_SYSTEM = """You are an OSINT investigation assistant embedded in Fieldwork, \
a graph-based intelligence platform. You have access to a snapshot of the \
investigation graph below. Answer the investigator's questions concisely and \
factually, grounded in the provided data. If the data doesn't contain enough \
information to answer, say so — do not speculate beyond what is shown. \
Use plain prose; avoid lists unless the question explicitly requests them."""


async def chat(
    message:  str,
    context:  str = "",
    history:  Optional[list[dict]] = None,
    model:    Optional[str] = None,
    timeout:  int = 120,
) -> str:
    """
    Multi-turn OSINT investigation chat grounded in graph context.

    history items: [{"role": "user"|"assistant", "content": str}, ...]
    context: structured text summary of case / graph data (injected as system context).
    """
    m = model or OLLAMA_MODEL
    system = _CHAT_SYSTEM
    if context:
        system += f"\n\n=== INVESTIGATION CONTEXT ===\n{context[:8000]}"

    # Build messages list for /api/chat
    messages: list[dict] = []
    for h in (history or []):
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": message})

    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. Claude (API → subscription bridge) — flatten history into one prompt
        try:
            convo = "\n".join(
                f"{'User' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
                for msg in messages
            )
            text, _engine = await claude_complete(
                system=system, user=convo, http=client,
                temperature=0.2, max_tokens=1024,
            )
            if text:
                return text.strip()
        except NoClaudeError:
            pass
        except Exception as exc:
            log.warning("Claude chat failed (%s) — using Ollama", exc)

        # 2. Ollama fallback
        payload = {
            "model":    m,
            "messages": messages,
            "stream":   False,
            "system":   system,
            "options":  {"temperature": 0.2, "num_predict": 1024},
        }
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


# ── Status / model management ─────────────────────────────────────────────────

async def ollama_status() -> dict:
    """Check Ollama availability and list pulled models."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {
                "available":     True,
                "models":        models,
                "default_model": OLLAMA_MODEL,
                "ollama_url":    OLLAMA_URL,
            }
    except Exception as exc:
        return {
            "available":     False,
            "models":        [],
            "default_model": OLLAMA_MODEL,
            "error":         str(exc),
        }


async def pull_model(model: str) -> None:
    """
    Pull a model from the Ollama registry.
    Blocks until the download is complete — call from a BackgroundTask.
    """
    log.info("Pulling Ollama model: %s", model)
    async with httpx.AsyncClient(timeout=1800) as client:
        # stream=True so Ollama streams progress; we consume and discard
        async with client.stream(
            "POST", f"{OLLAMA_URL}/api/pull", json={"name": model}
        ) as r:
            r.raise_for_status()
            async for _ in r.aiter_bytes():
                pass
    log.info("Model pull complete: %s", model)

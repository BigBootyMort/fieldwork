"""
NLP pipeline — Phase 4.3 / 4.4 / 4.5

Three capabilities, all optional-dependency-safe:

  extract_entities(text)          → dict of persons / orgs / locations / dates / money
  sentiment(text)                 → (score: float, label: str)
  detect_language(text)           → ISO 639-1 language code string
  translate_to_english(text, src) → translated string (no-op if LibreTranslate is down)
  process_text(text)              → single call that runs the full pipeline

spaCy is lazy-loaded on first call and reused for the process lifetime.
TextBlob and langdetect are imported inline so a missing package degrades
gracefully instead of crashing the entire backend.
"""
import logging
import os
from typing import Optional

log = logging.getLogger("fieldwork.ner")

# ── spaCy model (lazy singleton) ─────────────────────────────────────────────
_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        try:
            import spacy  # type: ignore
            log.info("Loading spaCy model en_core_web_sm …")
            _nlp = spacy.load("en_core_web_sm")
            log.info("spaCy model ready")
        except Exception as exc:
            log.warning("spaCy unavailable — NER disabled: %s", exc)
            _nlp = False   # sentinel: don't retry
    return _nlp if _nlp is not False else None


def warm_nlp() -> bool:
    """
    Call at application startup to pay the model-load cost once.
    Returns True if the model loaded successfully.
    """
    return _get_nlp() is not None


# ── NER ──────────────────────────────────────────────────────────────────────
def extract_entities(text: str) -> dict:
    """
    Run spaCy NER over text.
    Returns deduplicated entity lists keyed by category.
    Caps input at 80 000 chars for speed.
    """
    nlp = _get_nlp()
    if nlp is None:
        return {"persons": [], "orgs": [], "locations": [], "dates": [], "money": []}

    doc = nlp(text[:80_000])

    result: dict[str, list[str]] = {
        "persons":   [],
        "orgs":      [],
        "locations": [],
        "dates":     [],
        "money":     [],
    }
    seen: set[str] = set()

    for ent in doc.ents:
        key = ent.text.strip()
        if not key or key in seen or len(key) > 200:
            continue
        seen.add(key)

        if ent.label_ == "PERSON":
            result["persons"].append(key)
        elif ent.label_ == "ORG":
            result["orgs"].append(key)
        elif ent.label_ in ("GPE", "LOC", "FAC"):
            result["locations"].append(key)
        elif ent.label_ == "DATE":
            result["dates"].append(key)
        elif ent.label_ in ("MONEY", "PERCENT"):
            result["money"].append(key)

    return result


# ── Sentiment ─────────────────────────────────────────────────────────────────
def sentiment(text: str) -> tuple[float, str]:
    """
    TextBlob polarity on up to 10 000 chars.
    Returns (score, label) where score ∈ [-1.0, 1.0].
    Degrades to (0.0, 'neutral') if TextBlob is not installed.
    """
    try:
        from textblob import TextBlob  # type: ignore
        score = round(float(TextBlob(text[:10_000]).sentiment.polarity), 3)
    except Exception as exc:
        log.debug("TextBlob unavailable: %s", exc)
        return 0.0, "neutral"

    if score > 0.1:
        label = "positive"
    elif score < -0.1:
        label = "negative"
    else:
        label = "neutral"
    return score, label


# ── Language detection ────────────────────────────────────────────────────────
async def detect_language(text: str) -> str:
    """
    Detect ISO 639-1 language code using langdetect.
    Returns 'en' on any failure.
    """
    try:
        from langdetect import detect  # type: ignore
        return detect(text[:2_000]) or "en"
    except Exception:
        return "en"


# ── Translation (LibreTranslate) ──────────────────────────────────────────────
_LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "http://libretranslate:5000")


async def translate_to_english(text: str, source_lang: str) -> str:
    """
    Translate text to English via a local LibreTranslate instance.
    Returns the original text unchanged if the service is unavailable.
    LibreTranslate is opt-in — start the stack with --profile translation.
    """
    if source_lang == "en":
        return text

    import httpx  # already a project dep
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{_LIBRETRANSLATE_URL}/translate",
                json={
                    "q":      text[:15_000],
                    "source": source_lang,
                    "target": "en",
                    "format": "text",
                },
            )
            if r.status_code == 200:
                translated = r.json().get("translatedText", "")
                if translated:
                    return translated
    except Exception as exc:
        log.debug("LibreTranslate unavailable: %s", exc)

    return text   # fall back to original


# ── Full pipeline ─────────────────────────────────────────────────────────────
async def process_text(text: str, translate: bool = False) -> dict:
    """
    Run the complete NLP pipeline on a piece of text:
      1. Language detection
      2. Optional translation to English
      3. NER
      4. Sentiment

    Returns a single dict with all results so callers don't have to
    call each function individually.
    """
    if not text or not text.strip():
        return {
            "language":        "en",
            "translated":      False,
            "entities":        {"persons": [], "orgs": [], "locations": [], "dates": [], "money": []},
            "sentiment_score": 0.0,
            "sentiment_label": "neutral",
        }

    lang = await detect_language(text)

    working_text = text
    translated = False
    if translate and lang != "en":
        working_text = await translate_to_english(text, lang)
        translated = working_text != text

    entities           = extract_entities(working_text)
    sent_score, sent_label = sentiment(working_text)

    return {
        "language":        lang,
        "translated":      translated,
        "entities":        entities,
        "sentiment_score": sent_score,
        "sentiment_label": sent_label,
    }

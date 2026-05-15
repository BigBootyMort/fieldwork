"""
Article text fetcher + PDF extractor — Phase 4.1 / 4.2

fetch_article_text(url)  → plain text from a news article URL
extract_pdf_text(path)   → plain text from a local PDF file

Both return None on failure so callers can continue gracefully.
"""
import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("fieldwork.scraper")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_FETCH_TIMEOUT = 12.0
_MAX_CHARS     = 60_000   # cap stored text to keep Neo4j properties sane

# Ordered list of CSS selectors tried for main-content extraction.
# First selector that yields >300 chars wins.
_CONTENT_SELECTORS = [
    "article",
    '[role="main"]',
    "main",
    ".article-body",
    ".article-content",
    ".article__body",
    ".article__content",
    ".story-body",
    ".story-content",
    ".post-content",
    ".entry-content",
    ".content-body",
    ".page-content",
    "#article-body",
    "#main-content",
    "#content",
]

# Tags that carry no readable content — stripped before extraction
_NOISE_TAGS = [
    "script", "style", "nav", "header", "footer",
    "aside", "figure", "figcaption", "noscript",
    "iframe", "form", "button", "svg", "picture",
]


def _clean(text: str) -> str:
    """Collapse whitespace runs and enforce length cap."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:_MAX_CHARS]


async def fetch_article_text(url: str) -> str | None:
    """
    Download a news article and return its main body as plain text.
    Returns None on any failure (network error, non-HTML, too short, etc.).
    """
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            r = await client.get(url)

        if r.status_code != 200:
            log.debug("scraper: HTTP %s for %s", r.status_code, url)
            return None

        content_type = r.headers.get("content-type", "")
        if "html" not in content_type:
            log.debug("scraper: non-HTML content-type %r for %s", content_type, url)
            return None

        soup = BeautifulSoup(r.text, "lxml")

        # Remove noise elements in-place
        for tag in soup(_NOISE_TAGS):
            tag.decompose()

        # Try article-specific containers first
        for selector in _CONTENT_SELECTORS:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(" ", strip=True)
                if len(text) > 300:
                    return _clean(text)

        # Fallback: entire body
        body = soup.find("body")
        if body:
            text = body.get_text(" ", strip=True)
            if len(text) > 300:
                return _clean(text)

        return None

    except Exception as exc:
        log.debug("scraper: fetch failed for %s — %s", url, exc)
        return None


async def extract_pdf_text(path: str) -> str | None:
    """
    Extract plain text from a PDF file using pdfminer.six.
    Returns None if extraction fails or yields too little text.
    Falls back gracefully if pdfminer is not installed.
    """
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(path)
        if text and len(text.strip()) > 50:
            return _clean(text)
        return None
    except ImportError:
        log.debug("scraper: pdfminer not installed — PDF extraction skipped")
        return None
    except Exception as exc:
        log.debug("scraper: PDF extract failed for %s — %s", path, exc)
        return None

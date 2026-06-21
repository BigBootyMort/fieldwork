"""Adverse media screening — negative news search + LLM classification."""
import asyncio
import json
import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("fieldwork.adverse_media")

NEGATIVE_TERMS = [
    "fraud", "scam", "criminal", "convicted", "arrested", "indicted",
    "investigation", "scandal", "bankrupt", "lawsuit", "money laundering",
    "bribery", "corruption", "embezzlement", "extortion", "trafficking",
]

_DDG_URL = "https://html.duckduckgo.com/html/"
_QUERY_SUFFIX = "(fraud OR scam OR criminal OR arrested OR convicted OR investigation OR scandal)"


def _parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for r in soup.select(".result")[:8]:
        title_el = r.select_one(".result__title")
        snippet_el = r.select_one(".result__snippet")
        url_el = r.select_one(".result__url")
        title = title_el.get_text(strip=True) if title_el else ""
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        url = url_el.get_text(strip=True) if url_el else ""
        if title:
            results.append({"title": title, "snippet": snippet, "url": url})
    return results


async def _classify_article(
    client: httpx.AsyncClient,
    name: str,
    article: dict,
    ollama_url: str,
    model: str,
) -> dict | None:
    prompt = (
        f"Is this article headline/snippet about '{name}' in a clearly negative/adverse way "
        "(crime, fraud, scandal, legal trouble)? "
        'Reply with JSON: {"adverse": true/false, "category": "fraud|criminal|scandal|legal|financial|other|none", '
        '"confidence": "high|medium|low"}. '
        f"Headline: {article['title']}. Snippet: {article['snippet']}"
    )
    try:
        resp = await client.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=20.0,
        )
        raw = resp.json().get("response", "{}")
        # extract JSON from response
        m = re.search(r"\{[^}]+\}", raw)
        parsed = json.loads(m.group(0)) if m else {}
        if parsed.get("adverse"):
            return {
                "title": article["title"],
                "url": article["url"],
                "snippet": article["snippet"],
                "category": parsed.get("category", "other"),
                "confidence": parsed.get("confidence", "low"),
            }
    except Exception as exc:
        log.debug("Ollama classify error: %s", exc)
    return None


async def search_adverse_media(
    name: str,
    ollama_url: str = "http://ollama:11434",
    model: str = "llama3.2",
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                _DDG_URL,
                data={"q": f'"{name}" {_QUERY_SUFFIX}', "kl": "wt-wt"},
                headers={"User-Agent": "Mozilla/5.0 (compatible; Fieldwork OSINT)"},
            )
            articles = _parse_results(resp.text)

            tasks = [
                _classify_article(client, name, a, ollama_url, model)
                for a in articles
            ]
            results = await asyncio.gather(*tasks)

        adverse = [r for r in results if r is not None]
        categories = list({a["category"] for a in adverse})
        count = len(adverse)
        risk = "LOW" if count == 0 else ("MEDIUM" if count <= 2 else "HIGH")

        return {
            "name": name,
            "found": bool(adverse),
            "adverse_count": count,
            "risk_level": risk,
            "categories": categories,
            "articles": adverse,
        }
    except Exception as exc:
        log.warning("Adverse media error for %r: %s", name, exc)
        return {
            "name": name,
            "found": False,
            "adverse_count": 0,
            "risk_level": "LOW",
            "categories": [],
            "articles": [],
            "error": str(exc),
        }

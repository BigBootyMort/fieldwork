"""
NewsAPI crawler — finds recent news mentions of a person.

Phase 4 additions:
  • Fetches full article body via scraper.fetch_article_text
  • Detects language and optionally translates via ner_pipeline
  • Runs spaCy NER over full text → Person / Company nodes → MENTIONED_IN Article
  • Stores TextBlob sentiment score on the FEATURED_IN relationship

Free tier: 100 requests/day; results are development-only (not for
republication). Developer plan restricts results to the past month.
"""
import asyncio
import hashlib
import logging
import os
import re
from typing import Optional

import httpx
from aiolimiter import AsyncLimiter

from scraper import fetch_article_text
from ner_pipeline import process_text

log = logging.getLogger("crawler.newsapi")

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Normalise a name/org string to a Neo4j-safe ID
_slug_re = re.compile(r"[^a-z0-9]+")

def _slug(name: str) -> str:
    return _slug_re.sub("_", name.lower().strip()).strip("_")[:100]


class NewsCrawler:
    name = "newsapi"

    def __init__(self, graph_db):
        self.graph   = graph_db
        self.api_key = os.getenv("NEWS_API_KEY", "")
        # Free tier: 100 req/day — 1 req/2 s keeps well under that
        self.limiter = AsyncLimiter(max_rate=1, time_period=2)

    # ── Main entry point ──────────────────────────────────────────
    async def crawl(self, person: dict, company_hint: Optional[str] = None):
        if not self.api_key:
            log.info("NewsAPI: no key, skipping")
            return

        name  = person["name"]
        query = f'"{name}"'
        if company_hint:
            query += f' AND "{company_hint}"'

        # 1. Fetch article list from NewsAPI
        async with httpx.AsyncClient(timeout=20.0) as client:
            async with self.limiter:
                resp = await client.get(NEWSAPI_URL, params={
                    "q":        query,
                    "apiKey":   self.api_key,
                    "pageSize": 10,
                    "language": "en",
                    "sortBy":   "relevancy",
                })

        if resp.status_code == 401:
            log.error("NewsAPI: invalid key")
            return
        if resp.status_code == 426:
            log.warning("NewsAPI: plan upgrade required")
            return
        if resp.status_code != 200:
            log.warning("NewsAPI: HTTP %s", resp.status_code)
            return

        articles = resp.json().get("articles", [])
        log.info("NewsAPI: %d articles for %r", len(articles), name)
        if not articles:
            return

        # 2. Store article metadata nodes
        article_ids: list[tuple[str, str]] = []   # (article_id, url)
        async with self.graph.driver.session() as session:
            for article in articles[:10]:
                url   = (article.get("url") or "").strip()
                title = (article.get("title") or "").strip()
                if not url or not title or url == "https://removed.com":
                    continue

                article_id = hashlib.sha1(url.encode()).hexdigest()
                await session.run(
                    "MERGE (a:Article {id: $id}) "
                    "ON CREATE SET a.title = $title, a.url = $url, "
                    "              a.published_at = $published_at, "
                    "              a.outlet = $outlet, a.first_seen = datetime() "
                    "WITH a "
                    "MATCH (p:Person {id: $pid}) "
                    "MERGE (p)-[r:FEATURED_IN]->(a) "
                    "ON CREATE SET r.source = 'newsapi', r.first_seen = datetime()",
                    id=article_id,
                    title=title[:500],
                    url=url[:500],
                    published_at=(article.get("publishedAt") or ""),
                    outlet=((article.get("source") or {}).get("name") or ""),
                    pid=person["id"],
                )
                article_ids.append((article_id, url))
                log.info("  + %s -> [FEATURED_IN] -> %.60s", name, title)

        if not article_ids:
            return

        # 3. Fetch full text for all articles concurrently (10 s cap per article)
        texts = await asyncio.gather(
            *[self._fetch_with_timeout(url) for _, url in article_ids]
        )

        # 4. Run NLP pipeline + write graph enrichments
        await asyncio.gather(
            *[
                self._enrich_article(article_id, url, text, person)
                for (article_id, url), text in zip(article_ids, texts)
                if text
            ]
        )

    # ── Helpers ───────────────────────────────────────────────────
    @staticmethod
    async def _fetch_with_timeout(url: str) -> Optional[str]:
        try:
            return await asyncio.wait_for(fetch_article_text(url), timeout=12.0)
        except (asyncio.TimeoutError, Exception):
            return None

    async def _enrich_article(
        self,
        article_id: str,
        url: str,
        text: str,
        person: dict,
    ) -> None:
        """
        Given full article text:
          • Store full_text + language on the Article node
          • Store sentiment on the FEATURED_IN relationship
          • MERGE NER-extracted Person and Company nodes → MENTIONED_IN Article
        """
        try:
            nlp_result = await process_text(text, translate=False)
        except Exception as exc:
            log.warning("NLP failed for %s: %s", url, exc)
            return

        sent_score = nlp_result["sentiment_score"]
        sent_label = nlp_result["sentiment_label"]
        language   = nlp_result["language"]
        entities   = nlp_result["entities"]

        async with self.graph.driver.session() as session:
            # Update Article node with full text + language
            await session.run(
                "MATCH (a:Article {id: $id}) "
                "SET a.full_text = $text, a.language = $lang, "
                "    a.sentiment_score = $score, a.sentiment_label = $label",
                id=article_id,
                text=text[:50_000],
                lang=language,
                score=sent_score,
                label=sent_label,
            )

            # Update FEATURED_IN relationship with sentiment
            await session.run(
                "MATCH (p:Person {id: $pid})-[r:FEATURED_IN]->(a:Article {id: $aid}) "
                "SET r.sentiment_score = $score, r.sentiment_label = $label",
                pid=person["id"],
                aid=article_id,
                score=sent_score,
                label=sent_label,
            )

            # Write NER-extracted Person nodes → MENTIONED_IN Article
            subject_name = person["name"].lower()
            for extracted_name in entities.get("persons", [])[:20]:
                # Skip the subject themselves to avoid noise
                if extracted_name.lower() == subject_name:
                    continue
                pid = _slug(extracted_name)
                if not pid:
                    continue
                await session.run(
                    "MERGE (p:Person {id: $id}) "
                    "ON CREATE SET p.name = $name, p.source = 'ner', "
                    "              p.first_seen = datetime() "
                    "WITH p "
                    "MATCH (a:Article {id: $aid}) "
                    "MERGE (p)-[r:MENTIONED_IN]->(a) "
                    "ON CREATE SET r.source = 'ner', r.first_seen = datetime()",
                    id=pid, name=extracted_name, aid=article_id,
                )

            # Write NER-extracted Org/Company nodes → MENTIONED_IN Article
            for org_name in entities.get("orgs", [])[:20]:
                oid = _slug(org_name)
                if not oid:
                    continue
                await session.run(
                    "MERGE (c:Company {id: $id}) "
                    "ON CREATE SET c.name = $name, c.source = 'ner', "
                    "              c.first_seen = datetime() "
                    "WITH c "
                    "MATCH (a:Article {id: $aid}) "
                    "MERGE (c)-[r:MENTIONED_IN]->(a) "
                    "ON CREATE SET r.source = 'ner', r.first_seen = datetime()",
                    id=oid, name=org_name, aid=article_id,
                )

        log.info(
            "  NLP %s: lang=%s sentiment=%s persons=%d orgs=%d",
            article_id[:8],
            language,
            sent_label,
            len(entities.get("persons", [])),
            len(entities.get("orgs", [])),
        )

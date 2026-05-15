"""
Telegram public channel scraper — Phase 6.3

Scrapes the public web preview of Telegram channels (t.me/s/CHANNEL).
No login, no API key, no Telegram account required.
Only works on channels that have enabled "Join Link / Public" mode.

Runs NER over post content to extract and store Person / Company mentions.

  scrape_telegram_channel(graph_db, channel, pages)
      Scrape a public channel, store Post nodes, run NER on content.
"""
import asyncio
import hashlib
import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from ner_pipeline import process_text

log = logging.getLogger("crawler.telegram")

_BASE = "https://t.me/s"
_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def scrape_telegram_channel(
    graph_db,
    channel: str,
    pages: int = 3,
    keyword_filter: Optional[str] = None,
) -> dict:
    """
    Scrape up to *pages* pages of a public Telegram channel.

    Each page is the HTML preview at t.me/s/{channel}?before={msg_id}.
    Posts are stored as TelegramPost nodes.
    NER is run on the combined text and extracted entities are linked
    to the channel via MENTIONED_IN.

    keyword_filter: if set, only store posts that contain this string.
    """
    channel = channel.lstrip("@").strip()
    log.info("Telegram: scraping @%s (%d pages)", channel, pages)

    all_posts: list[dict] = []
    before_id: Optional[int] = None

    for page_num in range(pages):
        posts, next_before = await _scrape_page(channel, before_id)
        if posts is None:
            if page_num == 0:
                return {
                    "found": False,
                    "channel": channel,
                    "reason": (
                        "Channel not found, private, or has no public web preview. "
                        "Only public channels with web preview enabled are accessible."
                    ),
                }
            break  # partial results are fine

        if keyword_filter:
            posts = [p for p in posts if keyword_filter.lower() in p["text"].lower()]

        all_posts.extend(posts)
        before_id = next_before
        if not next_before:
            break
        await asyncio.sleep(1.5)  # polite crawl

    if not all_posts:
        return {
            "found": True,
            "channel": channel,
            "posts_scraped": 0,
            "message": "No posts found" + (f" matching '{keyword_filter}'" if keyword_filter else ""),
        }

    # Write posts to graph + run NER on combined text
    stored    = 0
    full_text = "\n\n".join(p["text"] for p in all_posts if p["text"])

    async with graph_db.driver.session() as session:
        # Upsert Channel node
        await session.run(
            "MERGE (ch:TelegramChannel {id: $id}) "
            "ON CREATE SET ch.name = $name, ch.source = 'telegram', ch.first_seen = datetime() "
            "ON MATCH  SET ch.last_scraped = datetime()",
            id=f"telegram:{channel.lower()}",
            name=channel,
        )

        for post in all_posts:
            post_id = hashlib.sha1(
                f"telegram:{channel}:{post['msg_id']}".encode()
            ).hexdigest()

            await session.run(
                "MERGE (p:TelegramPost {id: $id}) "
                "ON CREATE SET "
                "  p.channel   = $channel, "
                "  p.msg_id    = $msg_id, "
                "  p.text      = $text, "
                "  p.date      = $date, "
                "  p.url       = $url, "
                "  p.views     = $views, "
                "  p.source    = 'telegram', "
                "  p.first_seen = datetime() "
                "WITH p "
                "MATCH (ch:TelegramChannel {id: $ch_id}) "
                "MERGE (ch)-[:PUBLISHED]->(p)",
                id=post_id,
                channel=channel,
                msg_id=post["msg_id"],
                text=post["text"][:5000],
                date=post["date"],
                url=post["url"],
                views=post["views"],
                ch_id=f"telegram:{channel.lower()}",
            )
            stored += 1

    # NER on all scraped text → entities linked to channel
    entities_summary: dict = {}
    if full_text.strip():
        try:
            nlp = await process_text(full_text, translate=False)
            entities_summary = nlp["entities"]
            ch_id = f"telegram:{channel.lower()}"

            async with graph_db.driver.session() as session:
                for name in nlp["entities"].get("persons", [])[:30]:
                    pid = name.lower().replace(" ", "_")[:100]
                    await session.run(
                        "MERGE (p:Person {id: $id}) "
                        "ON CREATE SET p.name = $name, p.source = 'ner_telegram', p.first_seen = datetime() "
                        "WITH p MATCH (ch:TelegramChannel {id: $ch_id}) "
                        "MERGE (p)-[:MENTIONED_IN]->(ch)",
                        id=pid, name=name, ch_id=ch_id,
                    )
                for name in nlp["entities"].get("orgs", [])[:30]:
                    oid = name.lower().replace(" ", "_")[:100]
                    await session.run(
                        "MERGE (c:Company {id: $id}) "
                        "ON CREATE SET c.name = $name, c.source = 'ner_telegram', c.first_seen = datetime() "
                        "WITH c MATCH (ch:TelegramChannel {id: $ch_id}) "
                        "MERGE (c)-[:MENTIONED_IN]->(ch)",
                        id=oid, name=name, ch_id=ch_id,
                    )
        except Exception as exc:
            log.warning("Telegram NER failed: %s", exc)

    log.info("Telegram @%s: %d posts stored", channel, stored)
    return {
        "found":         True,
        "channel":       channel,
        "posts_scraped": stored,
        "entities":      entities_summary,
        "posts":         all_posts[:20],  # first 20 for UI preview
    }


async def _scrape_page(
    channel: str,
    before_id: Optional[int],
) -> tuple[Optional[list[dict]], Optional[int]]:
    """
    Scrape one page of a public Telegram channel.
    Returns (posts, next_before_id) or (None, None) on failure.
    """
    url    = f"{_BASE}/{channel}"
    params = {}
    if before_id:
        params["before"] = before_id

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            r = await client.get(url, params=params)

        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            log.warning("Telegram: HTTP %s for @%s", r.status_code, channel)
            return None, None

        # Detect private / unavailable channel
        if "tgme_page_extra" in r.text and "Channel" not in r.text:
            return None, None

        soup  = BeautifulSoup(r.text, "lxml")
        posts = []

        for msg_div in soup.select(".tgme_widget_message"):
            # Message ID from data-post attribute
            data_post = msg_div.get("data-post", "")
            msg_id    = int(data_post.split("/")[-1]) if "/" in data_post else 0

            # Text content (may contain nested spans/links)
            text_el = msg_div.select_one(".tgme_widget_message_text")
            text    = text_el.get_text("\n", strip=True) if text_el else ""

            # Date
            time_el = msg_div.select_one("time")
            date    = time_el.get("datetime", "") if time_el else ""

            # Post URL
            link_el = msg_div.select_one("a.tgme_widget_message_date")
            post_url = link_el.get("href", "") if link_el else ""

            # View count
            views_el = msg_div.select_one(".tgme_widget_message_views")
            views    = views_el.get_text(strip=True) if views_el else ""

            if not text and not post_url:
                continue

            posts.append({
                "msg_id": msg_id,
                "text":   text,
                "date":   date,
                "url":    post_url,
                "views":  views,
            })

        # The oldest post's ID is used as the cursor for the next page
        min_id = min((p["msg_id"] for p in posts if p["msg_id"] > 0), default=None)
        return posts, (min_id - 1 if min_id and min_id > 1 else None)

    except Exception as exc:
        log.warning("Telegram scrape failed for @%s: %s", channel, exc)
        return None, None

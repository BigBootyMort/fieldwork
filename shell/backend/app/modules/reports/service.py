"""
Reports service — aggregates news + case data, calls Ollama, persists reports.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from typing   import Any

log = logging.getLogger("reports.service")

# ── Prompts ──────────────────────────────────────────────────────────────────

REPORT_SYSTEM = """\
You are a senior intelligence analyst producing a structured executive brief.
Write in clear, professional prose with markdown formatting.
Be concise but comprehensive. Use bullet points for lists.
Do NOT hallucinate — only reference information explicitly given to you.
"""

REPORT_USER_TEMPLATE = """\
Generate an Intelligence Report for {date}.

## NEWS SIGNAL (last {window_h}h)
{news_block}

## FIELDWORK — ACTIVE CASES
{cases_block}

Produce a structured report with these sections:
1. **Executive Summary** (3-5 sentences — the most critical developments)
2. **Regional Hotspots** (per-region bullet points, ranked by heat score)
3. **Threat & Risk Indicators** (war/crisis/security topics only)
4. **Active Investigations** (from the Fieldwork cases provided)
5. **Recommended Actions** (what the analyst should focus on today)

Keep the total report under 600 words.
"""


class ReportsService:
    def __init__(self, deps: dict[str, Any]):
        self.driver   = deps["graph_db"]
        self.http     = deps["http"]
        self.settings = deps["settings"]
        # Re-use the Ollama client pattern from news module
        self._ollama_url   = self.settings.OLLAMA_URL.rstrip("/")
        self._ollama_model = self.settings.OLLAMA_MODEL
        self._cache: tuple[float, dict] | None = None   # (ts, report)

    # ── News data ─────────────────────────────────────────────────────────────
    async def _news_summary(self, window_h: int) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
        async with self.driver.session() as s:
            r = await s.run(
                """
                MATCH (a:NewsArticle)-[:MENTIONS_COUNTRY]->(c:NewsCountry)
                MATCH (a)-[:FROM]->(src:NewsSource)
                WHERE a.published_at >= $cutoff
                WITH c.iso AS iso, c.name AS country,
                     a.topic AS topic,
                     coalesce(src.weight, 0.7) *
                     coalesce(a.topic_weight, 1.0) *
                     exp(-1.0 * duration.inSeconds(a.published_at, datetime()).seconds / 43200.0)
                     AS heat
                WITH iso, country,
                     sum(heat) AS score,
                     collect(DISTINCT topic)[..3] AS topics,
                     count(*) AS cnt
                ORDER BY score DESC
                RETURN iso, country, round(score * 10) AS hot_score,
                       topics, cnt
                LIMIT 20
                """,
                cutoff=cutoff,
            )
            country_rows = await r.fetch(20)

        # Top headlines per dominant region
        async with self.driver.session() as s:
            r = await s.run(
                """
                MATCH (a:NewsArticle)-[:FROM]->(src:NewsSource)
                WHERE a.published_at >= $cutoff
                WITH a, coalesce(src.weight, 0.7) * coalesce(a.topic_weight, 1.0) AS w
                ORDER BY w DESC
                RETURN a.title AS title, a.topic AS topic
                LIMIT 10
                """,
                cutoff=cutoff,
            )
            headlines = await r.fetch(10)

        async with self.driver.session() as s:
            r = await s.run(
                """
                MATCH (a:NewsArticle)
                WHERE a.published_at >= $cutoff
                RETURN a.topic AS topic, count(*) AS cnt
                ORDER BY cnt DESC
                """,
                cutoff=cutoff,
            )
            topic_rows = await r.fetch(20)

        return {
            "countries":  [dict(row) for row in country_rows],
            "headlines":  [dict(row) for row in headlines],
            "by_topic":   [dict(row) for row in topic_rows],
            "total_articles": sum(r["cnt"] for r in country_rows),
        }

    # ── Case data (Fieldwork graph) ───────────────────────────────────────────
    async def _cases_summary(self) -> list[dict]:
        try:
            async with self.driver.session() as s:
                r = await s.run(
                    """
                    MATCH (c:Case)
                    OPTIONAL MATCH (c)-[:HAS_SUBJECT]->(n)
                    WITH c, count(DISTINCT n) AS subjects
                    OPTIONAL MATCH (c)-[:HAS_TASK]->(t:Task)
                    WITH c, subjects,
                         count(t) AS tasks,
                         sum(CASE WHEN t.done THEN 1 ELSE 0 END) AS done_tasks
                    RETURN c.id AS id, c.title AS title,
                           coalesce(c.status, 'open') AS status,
                           subjects, tasks, done_tasks,
                           c.created_at AS created_at
                    ORDER BY c.created_at DESC
                    LIMIT 8
                    """
                )
                rows = await r.fetch(8)
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("case query failed (Fieldwork data not available?): %s", exc)
            return []

    # ── Ollama call ───────────────────────────────────────────────────────────
    async def _llm(self, system: str, user: str) -> str:
        payload = {
            "model":   self._ollama_model,
            "stream":  False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "options": {"temperature": 0.25},
        }
        try:
            r = await self.http.post(
                f"{self._ollama_url}/api/chat",
                json=payload, timeout=180,
            )
            if r.status_code == 404:
                return (
                    f"_LLM unavailable — model `{self._ollama_model}` not loaded.  \n"
                    f"Run: `docker compose exec ollama ollama pull {self._ollama_model}`_"
                )
            r.raise_for_status()
            return (r.json().get("message") or {}).get("content", "").strip()
        except Exception as exc:
            return f"_LLM error: {exc}_"

    # ── Persist / retrieve ────────────────────────────────────────────────────
    async def _save_report(self, title: str, content: str, meta: dict) -> str:
        rid = "rep:" + hashlib.sha1(
            (title + str(time.time())).encode()
        ).hexdigest()[:20]
        async with self.driver.session() as s:
            await s.run(
                """
                CREATE (r:ReportDoc {
                    id:           $id,
                    title:        $title,
                    content:      $content,
                    article_count: $art_count,
                    case_count:   $case_count,
                    generated_at: datetime()
                })
                """,
                id=rid, title=title, content=content,
                art_count=meta.get("article_count", 0),
                case_count=meta.get("case_count", 0),
            )
        return rid

    async def list_reports(self) -> list[dict]:
        async with self.driver.session() as s:
            r = await s.run(
                """
                MATCH (r:ReportDoc)
                RETURN r.id AS id, r.title AS title,
                       r.generated_at AS generated_at,
                       r.article_count AS article_count,
                       r.case_count AS case_count,
                       left(r.content, 220) AS preview
                ORDER BY r.generated_at DESC
                LIMIT 30
                """
            )
            rows = await r.fetch(30)
        return [
            {
                "id":            rec["id"],
                "title":         rec["title"],
                "generated_at":  str(rec["generated_at"]) if rec["generated_at"] else "",
                "article_count": rec["article_count"],
                "case_count":    rec["case_count"],
                "preview":       rec["preview"] or "",
            }
            for rec in rows
        ]

    async def get_report(self, report_id: str) -> dict | None:
        async with self.driver.session() as s:
            r = await s.run(
                """
                MATCH (r:ReportDoc {id: $id})
                RETURN r.id AS id, r.title AS title, r.content AS content,
                       r.generated_at AS generated_at,
                       r.article_count AS article_count,
                       r.case_count AS case_count
                """,
                id=report_id,
            )
            rec = await r.single()
        if not rec:
            return None
        return {
            "id":            rec["id"],
            "title":         rec["title"],
            "content":       rec["content"],
            "generated_at":  str(rec["generated_at"]) if rec["generated_at"] else "",
            "article_count": rec["article_count"],
            "case_count":    rec["case_count"],
        }

    async def delete_report(self, report_id: str) -> bool:
        async with self.driver.session() as s:
            r = await s.run(
                "MATCH (r:ReportDoc {id:$id}) DELETE r RETURN count(r) AS n",
                id=report_id,
            )
            rec = await r.single()
        return bool(rec and rec["n"])

    # ── Main: generate ────────────────────────────────────────────────────────
    async def generate(self, window_h: int = 24, save: bool = True) -> dict:
        now = time.time()
        # 5-minute cache
        if self._cache and (now - self._cache[0]) < 300:
            return self._cache[1]

        news = await self._news_summary(window_h)
        cases = await self._cases_summary()

        # Build text blocks for the LLM
        news_block = self._format_news_block(news, window_h)
        cases_block = self._format_cases_block(cases)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        user_msg = REPORT_USER_TEMPLATE.format(
            date=date_str,
            window_h=window_h,
            news_block=news_block,
            cases_block=cases_block,
        )

        brief = await self._llm(REPORT_SYSTEM, user_msg)

        title = f"Intelligence Brief — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
        report = {
            "title":         title,
            "content":       brief,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "window_hours":  window_h,
            "article_count": news["total_articles"],
            "case_count":    len(cases),
            "news_summary":  news,
            "cases":         cases,
            "cached":        False,
        }

        if save and "LLM" not in brief[:20]:
            try:
                rid = await self._save_report(
                    title, brief,
                    {"article_count": news["total_articles"], "case_count": len(cases)},
                )
                report["id"] = rid
            except Exception as exc:
                log.warning("report save failed: %s", exc)

        self._cache = (now, report)
        return report

    # ── Formatting helpers ────────────────────────────────────────────────────
    @staticmethod
    def _format_news_block(news: dict, window_h: int) -> str:
        lines = [f"Window: {window_h}h | Total articles: {news['total_articles']}"]
        lines.append("")

        # Topics
        if news["by_topic"]:
            lines.append("Topics (article count):")
            for t in news["by_topic"][:8]:
                lines.append(f"  • {t['topic']}: {t['cnt']}")
            lines.append("")

        # Top countries
        if news["countries"]:
            lines.append("Top countries by heat score:")
            for c in news["countries"][:10]:
                topics_str = ", ".join(c.get("topics") or [])
                lines.append(
                    f"  • {c['country']} ({c['iso']}) — score {c['hot_score']}, "
                    f"{c['cnt']} articles, topics: {topics_str}"
                )
            lines.append("")

        # Top headlines
        if news["headlines"]:
            lines.append("Top headlines:")
            for h in news["headlines"][:8]:
                lines.append(f"  • [{h['topic']}] {h['title']}")

        return "\n".join(lines)

    @staticmethod
    def _format_cases_block(cases: list[dict]) -> str:
        if not cases:
            return "No active cases found in Fieldwork."
        lines = []
        for c in cases:
            tasks_str = ""
            if c.get("tasks"):
                tasks_str = f" | Tasks: {c['done_tasks']}/{c['tasks']} done"
            lines.append(
                f"  • [{c.get('status','open')}] {c.get('title','Untitled')} "
                f"— {c.get('subjects', 0)} subjects{tasks_str}"
            )
        return "\n".join(lines)

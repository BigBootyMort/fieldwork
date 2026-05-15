"""
Phase 7 — Background scheduler.

Uses APScheduler's AsyncIOScheduler to poll due WatchedSubjects every 15 minutes.
For each due watch it re-runs a lightweight set of crawlers then calls check_watch()
to diff the connection snapshot and generate Alert nodes.

The scheduler is started in main.py's lifespan and shut down cleanly on exit.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from monitor import due_watches, check_watch

log = logging.getLogger("fieldwork.scheduler")

_scheduler: AsyncIOScheduler | None = None

# Crawlers that give the best signal per second for monitoring.
# Imported lazily inside the job so startup isn't blocked.
_MONITOR_CRAWLERS = [
    ("crawlers.opencorporates", "OpenCorporatesCrawler"),
    ("crawlers.news",           "NewsCrawler"),
    ("crawlers.sec",            "SECCrawler"),
    ("crawlers.github",         "GitHubCrawler"),
]

_CRAWL_TIMEOUT = 60.0   # per-crawler timeout (seconds)
_POLL_INTERVAL = 15     # minutes between scheduler ticks


async def _run_crawlers_for(graph_db, person: dict) -> None:
    """Run the monitor crawler set for one person, tolerating failures."""
    import importlib
    for module_path, class_name in _MONITOR_CRAWLERS:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            crawler = cls(graph_db)
            await asyncio.wait_for(crawler.crawl(person, None), timeout=_CRAWL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Monitor: crawler %s timed out for '%s'", class_name, person.get("name"))
        except Exception as e:
            log.warning("Monitor: crawler %s failed for '%s': %s", class_name, person.get("name"), e)


async def _monitor_cycle(graph_db) -> dict:
    """
    One scheduler tick:
      1. Fetch all due WatchedSubjects.
      2. For each, re-crawl then snapshot-diff.
      3. Return summary stats.
    """
    due = await due_watches(graph_db)
    if not due:
        return {"checked": 0, "alerts": 0}

    log.info("Monitor cycle: %d watch(es) due", len(due))
    total_alerts = 0

    for watch in due:
        name = watch["name"]
        log.info("Monitor: checking '%s'", name)
        try:
            person = await graph_db.find_or_create_person(name)
            await _run_crawlers_for(graph_db, person)
            result = await check_watch(graph_db, watch)
            total_alerts += result["alerts"]
        except Exception:
            log.exception("Monitor: unhandled error for watch '%s'", name)

    log.info("Monitor cycle done: %d checked, %d alert(s) created", len(due), total_alerts)
    return {"checked": len(due), "alerts": total_alerts}


async def run_all_active(graph_db) -> dict:
    """
    Manual trigger: run the check cycle for ALL active watches immediately,
    regardless of their last_checked time. Used by the /monitor/run endpoint.
    """
    from monitor import all_active_watches
    watches = await all_active_watches(graph_db)
    if not watches:
        return {"checked": 0, "alerts": 0}

    log.info("Manual monitor run: %d active watch(es)", len(watches))
    total_alerts = 0

    for watch in watches:
        name = watch["name"]
        try:
            person = await graph_db.find_or_create_person(name)
            await _run_crawlers_for(graph_db, person)
            result = await check_watch(graph_db, watch)
            total_alerts += result["alerts"]
        except Exception:
            log.exception("Manual monitor: unhandled error for watch '%s'", name)

    return {"checked": len(watches), "alerts": total_alerts}


def start_scheduler(graph_db) -> AsyncIOScheduler:
    """
    Initialise and start the APScheduler instance.
    Called once from main.py lifespan; returns the scheduler so it can be
    shut down on exit.
    """
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _monitor_cycle,
        trigger=IntervalTrigger(minutes=_POLL_INTERVAL),
        args=[graph_db],
        id="monitor_cycle",
        replace_existing=True,
        max_instances=1,      # never run overlapping cycles
        coalesce=True,        # merge missed fires into one
    )
    _scheduler.start()
    log.info("Scheduler started — monitor polls every %d min", _POLL_INTERVAL)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")

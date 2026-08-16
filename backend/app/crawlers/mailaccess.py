"""
MailAccess email OSINT — https://github.com/KatrielMoses/MailAccess

Runs as a sibling container (see docker-compose `mailaccess:` service). We reach its
REST API at MAILACCESS_URL (default http://mailaccess:8000) and drive an investigation:

    POST /api/investigate  {email, force}   -> 202 {id, status:"pending"}  (or 200 cached)
    GET  /api/report/{id}                    -> full report once status is terminal

MailAccess sweeps thousands of platforms + breach sources, so an investigation can take
minutes. We start it, then poll with a hard wall-clock cap and degrade gracefully to a
"still running" reason rather than hang the request. MailAccess caches results in its own
SQLite, so a retry after a timeout returns the finished report cheaply.

No API key required (the tool needs none). Optional: set MAILACCESS_API_KEY on both the
container and here if you enable auth on the service.
"""
import asyncio
import logging
import os
import re
import time

import httpx

log = logging.getLogger("fieldwork.mailaccess")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Statuses MailAccess reports (matched case-insensitively). A run is "finished" when its
# status is neither a running-state nor a failure — we treat any other/unknown label as
# terminal so an unexpected status string can't make us poll forever.
_RUNNING = {"pending", "queued", "running", "in_progress", "processing",
            "started", "working", "scanning"}
_FAILED = {"failed", "error", "cancelled", "canceled"}

# Poll pacing / wall-clock cap. httpx per-request timeout is separate and short.
_POLL_INTERVAL = 3.0
_MAX_WALL = 100.0


def _base_url() -> str:
    return os.getenv("MAILACCESS_URL", "http://mailaccess:8000").rstrip("/")


def _headers() -> dict:
    h = {"Accept": "application/json", "User-Agent": "Fieldwork OSINT"}
    key = os.getenv("MAILACCESS_API_KEY", "")
    if key:
        h["X-API-Key"] = key
    return h


def _len(x) -> int:
    return len(x) if isinstance(x, (list, dict)) else 0


def _normalize(email: str, report: dict) -> dict:
    """Map the GET /api/report/{id} structure to our flat, UI-friendly dict.

    Headline scalars are top-level in the report. Counts are derived from the report's
    nested containers (verified against a live report): platform accounts from the
    platform-discovery buckets in `findings_by_module`, breaches from `findings` tagged to
    breach/leak modules, clusters + entities from `graph_data`."""
    fbm = report.get("findings_by_module") or {}
    findings = report.get("findings") or []
    graph = report.get("graph_data") or {}

    accounts = 0
    if isinstance(fbm, dict):
        for k, v in fbm.items():
            if isinstance(v, list) and (
                k.endswith("_platforms")
                or k in {"account_discovery", "social_links", "fediverse_discovery"}
            ):
                accounts += len(v)

    breaches = 0
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict):
                m = str(f.get("module_name") or "").lower()
                if "breach" in m or "leak" in m:
                    breaches += 1

    return {
        "email":                 email,
        "found":                 True,
        "source":                "mailaccess",
        "report_id":             report.get("id") or report.get("investigation_id") or "",
        "status":                str(report.get("status") or "complete"),
        "exposure_score":        report.get("exposure_score"),
        "credential_risk_score": report.get("credential_risk_score"),
        "credential_risk_band":  report.get("credential_risk_band") or "",
        "risk_level":            report.get("risk_level") or "",
        "confirmed_name":        report.get("confirmed_name") or report.get("name") or "",
        "accounts_found":        accounts,
        "breaches_found":        breaches,
        "clusters":              _len(graph.get("clusters")),
        "entities":              _len(graph.get("nodes")),
    }


def _is_finished(report: dict) -> bool:
    """A report is done when its status is a non-running, non-failed label, or when the
    exposure score is actually populated (key present with a null value = still running)."""
    status = str(report.get("status") or "").lower()
    if status in _RUNNING or status in _FAILED:
        return False
    if status:                                  # any other terminal/unknown label
        return True
    return report.get("exposure_score") is not None


async def enrich_email_mailaccess(email: str) -> dict:
    """Investigate an email via the MailAccess service. Returns the found/reason dict shape."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        return {"email": email, "found": False, "reason": "Invalid email address"}

    base = _base_url()
    started = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_headers()) as client:
            # 1) Start (or hit cache).
            r = await client.post(f"{base}/api/investigate",
                                  json={"email": email, "force": False})
            if r.status_code in (401, 403):
                return {"email": email, "found": False,
                        "reason": "MailAccess requires MAILACCESS_API_KEY"}
            r.raise_for_status()
            body = r.json()

            # The POST body is only a record stub — its scores are null even when cached
            # ("status":"complete"). The populated scores live in GET /api/report/{id}, so
            # we always resolve the id and read the full report; never normalize the stub.
            report_id = body.get("id") or body.get("investigation_id")
            if not report_id:
                return {"email": email, "found": False,
                        "reason": "MailAccess did not return an investigation id"}

            # 2) Read the report — check immediately (cached results return fast), then poll
            #    every _POLL_INTERVAL until finished or the wall-clock cap.
            first = True
            while True:
                if not first:
                    await asyncio.sleep(_POLL_INTERVAL)
                first = False

                rr = await client.get(f"{base}/api/report/{report_id}")
                if rr.status_code != 404:
                    rr.raise_for_status()
                    report = rr.json()
                    status = str(report.get("status") or "").lower()
                    if status in _FAILED:
                        return {"email": email, "found": False, "report_id": report_id,
                                "reason": f"MailAccess investigation {status}"}
                    if _is_finished(report):
                        return _normalize(email, report)

                if time.monotonic() - started >= _MAX_WALL:
                    return {"email": email, "found": False, "report_id": report_id,
                            "reason": "Timed out — still running; MailAccess caches, retry shortly"}

    except httpx.HTTPStatusError as exc:
        log.warning("MailAccess HTTP error for %s: %s", email, exc)
        return {"email": email, "found": False, "reason": f"MailAccess HTTP {exc.response.status_code}"}
    except Exception as exc:
        log.warning("MailAccess lookup failed for %s: %s", email, exc)
        return {"email": email, "found": False, "reason": str(exc)}

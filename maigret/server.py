"""
Thin FastAPI wrapper around the Maigret 0.6.x CLI.

Accepts POST /search {"username": "...", "top_sites": 100, "timeout": 15}
Runs maigret as a subprocess, parses the JSON output, returns only
Claimed accounts. Running as a subprocess (rather than importing maigret
as a library) keeps us insulated from its internal API changes.

maigret 0.6.x CLI differences vs 0.4.x:
  - `--json simple` writes report_{username}_simple.json inside --folderoutput
  - The JSON already contains only Claimed accounts (no client-side filter needed)
  - URL is at entry["status"]["url"]  (was entry["url_user"])
  - Tags are at entry["status"]["tags"]
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("maigret-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="Maigret service", version="2.0.0")

# Only printable ASCII minus shell-special chars
_HANDLE_RE = re.compile(r"^[a-zA-Z0-9._\-]{1,50}$")


class SearchRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    top_sites: int = Field(100, ge=10, le=1000)
    timeout: int = Field(15, ge=5, le=60)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/search")
async def search(req: SearchRequest):
    if not _HANDLE_RE.match(req.username):
        raise HTTPException(400, "Username contains invalid characters")

    # Outer wall-clock limit
    outer_timeout = min(int(req.top_sites / 20 * req.timeout) + 30, 180)

    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "maigret",
            req.username,
            "--folderoutput", tmpdir,
            "--json", "simple",
            "--timeout", str(req.timeout),
            "--top-sites", str(req.top_sites),
            "--no-color",
            "--retries", "1",
        ]

        log.info(
            "Starting maigret for %r (top_sites=%d timeout=%d outer=%ds)",
            req.username, req.top_sites, req.timeout, outer_timeout,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await asyncio.wait_for(proc.wait(), timeout=float(outer_timeout))
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("Maigret timed out for %r after %ds", req.username, outer_timeout)
            raise HTTPException(504, f"Maigret timed out after {outer_timeout}s")

        # maigret 0.6.x: report_{username}_simple.json (slashes → underscores)
        safe_username = req.username.replace("/", "_")
        output_path = Path(tmpdir) / f"report_{safe_username}_simple.json"

        if not output_path.exists():
            # Fall back: scan for any *_simple.json in case naming differs
            candidates = list(Path(tmpdir).glob("*_simple.json"))
            if candidates:
                output_path = candidates[0]
            else:
                log.warning("Maigret produced no output file for %r", req.username)
                return {"username": req.username, "accounts": [], "count": 0}

        with open(output_path, encoding="utf-8") as f:
            raw = json.load(f)

    # 0.6.x "simple" JSON: {siteName: {status: {url, status, tags, ids}, site: {...}, ...}}
    # All entries are already Claimed — no status filter needed.
    accounts = []
    for site_name, info in raw.items():
        if not isinstance(info, dict):
            continue

        status_block = info.get("status") or {}
        if isinstance(status_block, dict):
            url  = str(status_block.get("url") or "").strip()
            tags = status_block.get("tags") or []
        else:
            # Unexpected shape — try legacy fields
            url  = str(info.get("url_user") or "").strip()
            tags = info.get("tags") or []

        # Category: try site block first, fall back to first tag
        site_block = info.get("site") or {}
        if isinstance(site_block, dict):
            category = site_block.get("category") or (tags[0] if tags else "")
        else:
            category = tags[0] if tags else ""

        accounts.append({
            "site":     site_name,
            "url":      url,
            "category": category,
            "tags":     tags if isinstance(tags, list) else [],
        })

    log.info("Maigret found %d accounts for %r", len(accounts), req.username)
    return {"username": req.username, "accounts": accounts, "count": len(accounts)}

"""
Thin FastAPI wrapper around theHarvester CLI.

Accepts POST /harvest {"domain": "example.com", "sources": "google,bing,..."}
Runs theHarvester as a subprocess, parses JSON output, returns structured
emails / hosts / IPs so the backend can promote them to Neo4j.

Sources that work without API keys (used by default):
  google, bing, duckduckgo, baidu, yahoo, crtsh, dnsdumpster,
  hackertarget, otx, rapiddns, sitedossier, urlscan
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("harvester-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="theHarvester service", version="1.0.0")

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

FREE_SOURCES = (
    "crtsh,dnsdumpster,hackertarget,otx,rapiddns,sitedossier,urlscan"
)


class HarvestRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)
    sources: str = Field(FREE_SOURCES, max_length=500)
    limit: int = Field(100, ge=10, le=500)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/harvest")
async def harvest(req: HarvestRequest):
    domain = req.domain.lower().strip()
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(400, "Invalid domain name")

    # Sanitise sources: only alphanumeric + commas
    sources = re.sub(r"[^a-zA-Z0-9,_]", "", req.sources)

    with tempfile.TemporaryDirectory() as tmpdir:
        output_base = os.path.join(tmpdir, "results")
        cmd = [
            "theHarvester",
            "-d", domain,
            "-b", sources,
            "-l", str(req.limit),
            "-f", output_base,
        ]

        log.info("Harvesting %r with sources: %s", domain, sources)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "theHarvester timed out after 120s")

        json_path = output_base + ".json"
        if not os.path.exists(json_path):
            log.warning("No output file produced for %r", domain)
            return {"domain": domain, "emails": [], "hosts": [], "ips": []}

        with open(json_path, encoding="utf-8") as f:
            raw = json.load(f)

    # theHarvester 4.x JSON schema
    emails = _normalise_list(raw.get("emails") or raw.get("email") or [])
    hosts  = _normalise_list(raw.get("hosts")  or raw.get("host")  or [])
    ips    = _normalise_list(raw.get("ips")     or raw.get("ip")    or [])

    # hosts entries sometimes carry an IP suffix "hostname:ip" — split them
    clean_hosts, extra_ips = [], []
    for h in hosts:
        if ":" in h:
            parts = h.split(":", 1)
            clean_hosts.append(parts[0].strip())
            extra_ips.append(parts[1].strip())
        else:
            clean_hosts.append(h.strip())
    ips = list(dict.fromkeys(ips + extra_ips))  # deduplicate, preserve order

    log.info(
        "Harvest %r: emails=%d hosts=%d ips=%d",
        domain, len(emails), len(clean_hosts), len(ips),
    )
    return {
        "domain": domain,
        "emails": emails[:200],
        "hosts":  clean_hosts[:200],
        "ips":    ips[:100],
    }


def _normalise_list(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if x]
    if isinstance(raw, str):
        return [x.strip() for x in raw.splitlines() if x.strip()]
    return []

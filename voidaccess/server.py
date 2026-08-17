"""
Thin FastAPI wrapper around the VoidAccess CLI (KatrielMoses/voidaccess), SQLite mode.

POST /search {"query": "...", "depth": "shallow", "use_tor": true, "use_llm": false}
    Runs `voidaccess investigate "<query>" --quiet --format json --output <tmpdir> ...`,
    which writes a single <slug>-<ts>.json report into <tmpdir>; we read and return it.
    (Confirmed against `voidaccess investigate --help` inside the image: investigate emits
    JSON directly, so no separate `export` step is needed.)

Everything degrades to a reason string; never raises to the caller beyond the timeout 504.
"""
import asyncio
import glob
import json
import logging
import os
import tempfile

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("voidaccess-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="VoidAccess service", version="1.1.0")

_INVESTIGATE_TIMEOUT = 300.0   # dark-web sweeps are slow; hard outer cap (shallow depth)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=300)
    depth: str = Field("shallow", pattern="^(shallow|normal|deep)$")
    use_tor: bool = True
    use_llm: bool = False


@app.get("/health")
async def health():
    tor_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--socks5-hostname", "127.0.0.1:9050", "-m", "6",
            "https://check.torproject.org/api/ip",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        o, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        tor_ok = proc.returncode == 0 and b"IsTor" in o
    except Exception:
        tor_ok = False
    return {"ok": True, "tor": tor_ok}


@app.post("/search")
async def search(req: SearchRequest):
    with tempfile.TemporaryDirectory(dir="/data") as outdir:
        cmd = ["voidaccess", "--no-banner", "investigate", req.query,
               "--depth", req.depth, "--quiet", "--format", "json", "--output", outdir]
        if not req.use_tor:
            cmd.append("--no-tor")
        if not req.use_llm:
            cmd.append("--no-llm")
        log.info("voidaccess investigate: %r depth=%s tor=%s llm=%s",
                 req.query, req.depth, req.use_tor, req.use_llm)

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd="/data",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=_INVESTIGATE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, f"voidaccess timed out after {int(_INVESTIGATE_TIMEOUT)}s")

        files = sorted(glob.glob(os.path.join(outdir, "*.json")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            detail = err.decode("utf-8", "replace")[-400:]
            log.warning("voidaccess produced no JSON for %r. rc=%s stderr=%s",
                        req.query, proc.returncode, detail)
            return {"query": req.query, "found": False,
                    "reason": "voidaccess produced no report (investigation failed?)"}

        try:
            with open(files[0], encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            return {"query": req.query, "found": False,
                    "reason": f"could not read voidaccess report: {exc}"}

    return {"query": req.query, "found": True,
            "investigation_id": data.get("id", ""), "data": data}

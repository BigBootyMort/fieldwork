"""
Thin FastAPI wrapper around the TorBot CLI (DedSecInside/TorBot).

POST /search {"url": "...", "depth": 1}
    Runs `python main.py -u <url> --depth <depth> -q --visualize json` as a subprocess
    (Tor SOCKS5 proxy at 127.0.0.1:9050 by default) and parses the link-tree JSON that
    TorBot's `tree.showJSON()` prints to stdout. Returns a flattened, de-duplicated list
    of discovered links plus the raw tree.

Running TorBot as a subprocess (not importing it) insulates us from its internal API.
"""
import asyncio
import json
import logging
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("torbot-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="TorBot service", version="1.0.0")

_URL_RE = re.compile(r"^https?://", re.I)
_TORBOT_DIR = "/torbot"
_CRAWL_TIMEOUT = 170.0  # outer wall-clock cap for a crawl


class CrawlRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    depth: int = Field(1, ge=1, le=3)


@app.get("/health")
async def health():
    """Report liveness + whether Tor has a working circuit."""
    tor_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--socks5-hostname", "127.0.0.1:9050", "-m", "6",
            "https://check.torproject.org/api/ip",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        tor_ok = proc.returncode == 0 and b"IsTor" in out
    except Exception:
        tor_ok = False
    return {"ok": True, "tor": tor_ok}


def _collect_labels(obj, acc: list) -> None:
    """Walk TorBot's tree JSON in document order, collecting node labels.

    TorBot keys the tree by page title / HTTP status (NOT URLs), e.g.
    {"Example Domain": {"children": ["301 Moved Permanently"]}} — so we gather every
    non-structural key and every leaf string. The first label collected is the crawled
    page (root); the rest are discovered links, in order."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "children":
                _collect_labels(v, acc)
            else:
                if isinstance(k, str):
                    acc.append(k)
                _collect_labels(v, acc)
    elif isinstance(obj, list):
        for it in obj:
            _collect_labels(it, acc)
    elif isinstance(obj, str):
        acc.append(obj)


def _parse_tree(stdout: str):
    """TorBot prints the tree JSON then a trailing blank line. Recover the JSON robustly."""
    text = stdout.strip()
    for candidate in (text, text.split("\n\n")[0].strip()):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    m = re.search(r"[\{\[].*[\}\]]", text, re.S)  # first JSON-looking block
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


@app.post("/search")
async def search(req: CrawlRequest):
    if not _URL_RE.match(req.url):
        raise HTTPException(400, "URL must start with http:// or https://")

    cmd = ["python", "main.py", "-u", req.url, "--depth", str(req.depth),
           "-q", "--visualize", "json"]
    log.info("TorBot crawl: url=%r depth=%d", req.url, req.depth)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=_TORBOT_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_CRAWL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"TorBot timed out after {int(_CRAWL_TIMEOUT)}s")

    tree = _parse_tree(out.decode("utf-8", "replace"))
    if tree is None:
        detail = err.decode("utf-8", "replace")[:400]
        log.warning("TorBot produced no parseable JSON for %r. stderr=%s", req.url, detail)
        return {"url": req.url, "depth": req.depth, "count": 0, "links": [],
                "reason": "TorBot returned no parseable results (site unreachable via Tor?)"}

    labels: list = []
    _collect_labels(tree, labels)
    root_title = labels[0] if labels else ""
    nodes = labels[1:]  # everything after the crawled page = discovered links
    return {
        "url":    req.url,
        "depth":  req.depth,
        "title":  root_title,
        "count":  len(nodes),
        "nodes":  nodes[:200],
        "tree":   tree,
    }

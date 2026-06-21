"""
Fieldwork Recon microservice — wraps CLI security tools via HTTP.

Endpoints:
  GET  /health       — liveness + tool availability
  POST /nmap         — port & service scan (nmap)
  POST /subfinder    — passive subdomain enumeration
  POST /nuclei       — vulnerability template scan
  POST /httpx        — HTTP probe & tech fingerprint
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import xml.etree.ElementTree as ET
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("recon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Fieldwork Recon Service", version="0.1.0")


# ── helpers ───────────────────────────────────────────────────────────────────

def _which(name: str) -> bool:
    return shutil.which(name) is not None


async def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run a subprocess, enforcing a wall-clock timeout."""
    log.info("running: %s", " ".join(cmd[:6]))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, f"Tool timed out after {timeout}s")
    rc = proc.returncode
    return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# ── request models ────────────────────────────────────────────────────────────

class NmapRequest(BaseModel):
    target:  str = Field(..., min_length=1, max_length=500)
    ports:   str = Field("1-1000", max_length=200)
    timing:  int = Field(4, ge=0, le=5)
    scripts: bool = Field(False, description="Run default NSE scripts (-sC)")


class SubfinderRequest(BaseModel):
    domain:      str  = Field(..., min_length=1, max_length=253)
    all_sources: bool = Field(False, description="Use all passive sources (-all)")


class NucleiRequest(BaseModel):
    target:    str           = Field(..., min_length=1, max_length=500)
    severity:  str           = Field("medium,high,critical", max_length=100)
    templates: Optional[str] = Field(None, max_length=500,
                                     description="Comma-separated template paths / tags")


class HttpxRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=500)


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    tools = {t: _which(t) for t in ["nmap", "subfinder", "nuclei", "httpx"]}
    return {"ok": True, "tools": tools}


# ── /nmap ─────────────────────────────────────────────────────────────────────

def _parse_nmap_xml(xml_data: str) -> list[dict]:
    """Parse nmap XML output (-oX -) into a list of port dicts."""
    ports: list[dict] = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        log.warning("nmap XML parse error: %s", exc)
        return ports

    for host in root.findall("host"):
        addr_el = host.find("address[@addrtype='ipv4']")
        host_addr = addr_el.get("addr", "") if addr_el is not None else ""

        ports_el = host.find("ports")
        if ports_el is None:
            continue
        for port_el in ports_el.findall("port"):
            state_el   = port_el.find("state")
            service_el = port_el.find("service")
            script_els = port_el.findall("script")

            scripts = [
                {"id": s.get("id", ""), "output": s.get("output", "")[:400]}
                for s in script_els
            ]

            ports.append({
                "host":      host_addr,
                "port":      int(port_el.get("portid", 0)),
                "protocol":  port_el.get("protocol", "tcp"),
                "state":     state_el.get("state", "unknown") if state_el is not None else "unknown",
                "reason":    state_el.get("reason", "") if state_el is not None else "",
                "service":   service_el.get("name", "")      if service_el is not None else "",
                "product":   service_el.get("product", "")   if service_el is not None else "",
                "version":   service_el.get("version", "")   if service_el is not None else "",
                "extrainfo": service_el.get("extrainfo", "") if service_el is not None else "",
                "scripts":   scripts,
            })
    return ports


@app.post("/nmap")
async def nmap_scan(req: NmapRequest):
    if not _which("nmap"):
        raise HTTPException(503, "nmap not installed")

    cmd = [
        "nmap", "-oX", "-",
        f"-T{req.timing}",
        "-p", req.ports,
        "-sV",                 # service/version detection
    ]
    if req.scripts:
        cmd += ["-sC"]         # default NSE scripts
    cmd.append(req.target)

    rc, stdout, stderr = await _run(cmd, timeout=300)
    ports = _parse_nmap_xml(stdout)
    open_ports  = [p for p in ports if p["state"] == "open"]
    closed_ports = [p for p in ports if p["state"] == "closed"]

    return {
        "target":        req.target,
        "ports":         ports,
        "open_count":    len(open_ports),
        "closed_count":  len(closed_ports),
        "open_ports":    open_ports,
        "returncode":    rc,
        "stderr_snippet": stderr[-500:] if stderr else "",
    }


# ── /subfinder ────────────────────────────────────────────────────────────────

@app.post("/subfinder")
async def subfinder_scan(req: SubfinderRequest):
    if not _which("subfinder"):
        raise HTTPException(503, "subfinder not installed")

    cmd = ["subfinder", "-d", req.domain, "-silent", "-json"]
    if req.all_sources:
        cmd.append("-all")

    rc, stdout, stderr = await _run(cmd, timeout=180)
    subdomains: list[dict] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            subdomains.append({
                "host":   obj.get("host", ""),
                "source": obj.get("source", ""),
                "ip":     obj.get("ip", ""),
            })
        except json.JSONDecodeError:
            # plain-text fallback (older subfinder versions)
            subdomains.append({"host": line, "source": "unknown", "ip": ""})

    return {
        "domain":     req.domain,
        "subdomains": subdomains,
        "count":      len(subdomains),
        "returncode": rc,
    }


# ── /nuclei ───────────────────────────────────────────────────────────────────

@app.post("/nuclei")
async def nuclei_scan(req: NucleiRequest):
    if not _which("nuclei"):
        raise HTTPException(503, "nuclei not installed")

    cmd = [
        "nuclei",
        "-u", req.target,
        "-severity", req.severity,
        "-json",                 # JSON-lines output (one JSON per line)
        "-silent",
        "-timeout", "15",
        "-bulk-size", "25",
        "-concurrency", "10",
    ]
    if req.templates:
        cmd += ["-t", req.templates]

    rc, stdout, stderr = await _run(cmd, timeout=360)
    findings: list[dict] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            info = obj.get("info", {})
            findings.append({
                "template_id":  obj.get("template-id", ""),
                "name":         info.get("name", ""),
                "severity":     info.get("severity", ""),
                "matched_at":   obj.get("matched-at", ""),
                "type":         obj.get("type", ""),
                "description":  info.get("description", ""),
                "reference":    info.get("reference", []),
                "tags":         info.get("tags", []),
                "curl_command": obj.get("curl-command", ""),
            })
        except json.JSONDecodeError:
            pass

    return {
        "target":     req.target,
        "findings":   findings,
        "count":      len(findings),
        "returncode": rc,
    }


# ── /httpx ────────────────────────────────────────────────────────────────────

@app.post("/httpx")
async def httpx_probe(req: HttpxRequest):
    if not _which("httpx"):
        raise HTTPException(503, "httpx not installed")

    # Note: httpx v1.6.9 has a bug where -json produces no output.
    # Plain-text output works. Format per line:
    #   URL [STATUS] [TITLE] [WEBSERVER] [FINAL_URL] [TECH,TECH2,...]
    # -no-color and -r flags suppress output in v1.6.9 — do not use them.
    cmd = [
        "httpx",
        "-u", req.target,
        "-title",
        "-status-code",
        "-content-length",
        "-tech-detect",
        "-web-server",
        "-follow-redirects",
        "-timeout", "20",
    ]

    rc, stdout, stderr = await _run(cmd, timeout=90)
    results: list[dict] = []

    import re as _re

    for line in stdout.splitlines():
        # Strip ANSI escape codes left by httpx
        line = _re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not line or "projectdiscovery" in line.lower():
            continue

        # Extract URL (text before the first bracket group)
        url_part = _re.split(r'\s+\[', line)[0].strip()
        if not url_part or not url_part.startswith("http"):
            continue

        # Extract all [...] groups (ANSI already stripped)
        brackets = _re.findall(r'\[([^\]]*)\]', line)

        # Status: first bracket that is digits (may be "301,200" — take last)
        status = 0
        final_url = ""
        title = ""
        webserver = ""
        technologies: list[str] = []

        for b in brackets:
            # Status codes: pure digits or "301,200" chain — take the last code
            if _re.match(r'^[\d,]+$', b) and not status:
                try:
                    status = int(b.split(",")[-1])
                except ValueError:
                    pass
            # Content-length: pure integer (large number)
            elif b.isdigit() and int(b) > 100:
                try:
                    content_length = int(b)
                except ValueError:
                    pass
            # Final URL after redirects
            elif b.startswith("http") and not final_url:
                final_url = b
            # Technologies: comma-separated non-URL group
            elif "," in b and not b.startswith("http") and not technologies:
                technologies = [t.strip() for t in b.split(",") if t.strip()]
            # Webserver: short label, no spaces, not a pure number
            elif " " not in b and len(b) < 60 and not b.isdigit() and not webserver and b:
                webserver = b
            # Title: longest remaining bracket with spaces (human-readable)
            elif not title and " " in b and len(b) > 5:
                title = b

        # Fix character encoding artifacts from httpx ANSI-stripped output
        title = title.encode('latin-1', errors='replace').decode('utf-8', errors='replace') \
                if 'Â' in title else title

        results.append({
            "url":            url_part,
            "status_code":    status,
            "title":          title,
            "content_length": content_length if 'content_length' in dir() else 0,
            "technologies":   technologies,
            "final_url":      final_url or url_part,
            "webserver":      webserver,
            "cdn":            "",
            "tls":            {},
        })

    return {
        "target":     req.target,
        "results":    results,
        "returncode": rc,
    }

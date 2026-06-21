"""
FastAPI routes for the Agent module — three operational domains:
  1. Software Development & Bug Bounties
  2. Freelance Task Execution
  3. Financial Trading

Config is stored in agent_config.json (same directory as this file, live-mounted to host)
so values survive container restarts without touching docker-compose.yml.
Settings can be updated at runtime via POST /api/agent/settings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from llm_bridge import claude_complete, NoClaudeError  # shared Claude API + bridge

log = logging.getLogger("agent")

# ── Persistent config (agent_config.json next to this file) ─────────────────
_CONFIG_FILE: Path = Path(__file__).parent / "agent_config.json"
_AGENT_CONFIG: Dict[str, str] = {}


def _load_config() -> Dict[str, str]:
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("agent: could not load config: %s", exc)
    return {}


def _save_config(data: Dict[str, str]) -> None:
    try:
        _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("agent: could not save config: %s", exc)


def _get_cfg(key: str, default: str = "") -> str:
    """Read from saved config → process env → default. Always live."""
    return _AGENT_CONFIG.get(key) or os.environ.get(key) or default


# Load persisted config at import time
_AGENT_CONFIG.update(_load_config())

# ── Settings registry (what the UI exposes) ──────────────────────────────────
_SETTINGS_REGISTRY = [
    {
        "key":     "ANTHROPIC_API_KEY",
        "group":   "AI Models",
        "icon":    "🤖",
        "label":   "Anthropic API Key",
        "note":    "Powers Claude AI analysis in Markets. Get yours at console.anthropic.com — claude-3-5-haiku is very affordable (~$0.01/analysis).",
        "url":     "https://console.anthropic.com/settings/keys",
        "kind":    "password",
    },
    {
        "key":     "OPENAI_API_KEY",
        "group":   "AI Models",
        "icon":    "🤖",
        "label":   "OpenAI API Key",
        "note":    "Alternative to Claude. Uses gpt-4o-mini for affordable analysis.",
        "url":     "https://platform.openai.com/api-keys",
        "kind":    "password",
    },
    {
        "key":     "CLAUDE_BRIDGE_URL",
        "group":   "AI Models",
        "icon":    "🌉",
        "label":   "Claude Code bridge URL",
        "note":    "Use your Claude Pro/Max subscription instead of an API key. "
                   "Run shell/host-bridge/start-claude-bridge.bat on your host (after "
                   "`claude setup-token`). Leave blank for the default "
                   "http://host.docker.internal:8088. Set CLAUDE_BRIDGE_ENABLED=off to disable.",
        "url":     "",
        "kind":    "text",
    },
    {
        "key":     "GITHUB_TOKEN",
        "group":   "GitHub",
        "icon":    "🐙",
        "label":   "Personal Access Token",
        "note":    "Needs repo + read:org scopes. Free with any GitHub account.",
        "url":     "https://github.com/settings/tokens/new?scopes=repo,read:org",
        "kind":    "password",
    },
    {
        "key":     "ALPACA_API_KEY",
        "group":   "Alpaca Trading",
        "icon":    "📈",
        "label":   "API Key",
        "note":    "From your Alpaca dashboard (paper or live account).",
        "url":     "https://app.alpaca.markets",
        "kind":    "password",
    },
    {
        "key":     "ALPACA_API_SECRET",
        "group":   "Alpaca Trading",
        "icon":    "📈",
        "label":   "API Secret",
        "note":    "Keep this private — never share it.",
        "url":     "https://app.alpaca.markets",
        "kind":    "password",
    },
    {
        "key":     "ALPACA_BASE_URL",
        "group":   "Alpaca Trading",
        "icon":    "📈",
        "label":   "Broker endpoint",
        "note":    "paper-api = safe simulation · api = real money",
        "url":     None,
        "kind":    "text",
        "default": "https://paper-api.alpaca.markets",
    },
]

# ── In-process task store ────────────────────────────────────────────────────
TASKS:      Dict[str, Dict[str, Any]] = {}
LOG_QUEUES: Dict[str, asyncio.Queue]  = {}
INT_QUEUES: Dict[str, asyncio.Queue]  = {}

# ── System prompt ────────────────────────────────────────────────────────────
PLAN_SYSTEM = """You are an autonomous AI Agent planning engine with three operational domains:
  1. SOFTWARE DEVELOPMENT & BUG BOUNTIES
  2. FREELANCE TASK EXECUTION
  3. FINANCIAL TRADING

Given an objective, respond ONLY with a JSON execution plan — no prose outside the JSON.

Top-level format:
{
  "summary": "One-sentence description of overall approach",
  "steps": [ <step objects> ]
}

Step object:
{
  "id": 1,
  "description": "Clear, specific action description",
  "action_type": "<see below>",
  "action": "<primary argument>",
  "content": "<secondary payload or omit>",
  "trade": null,
  "requires_intercept": false,
  "intercept_category": null,
  "intercept_reason": null
}

action_type reference:
  bash         — shell command in workspace        (action = command string)
  web_request  — HTTP GET                          (action = URL)
  analysis     — LLM reasoning step                (action = prompt; {{prev_result}} pastes prior output)
  report       — write formatted output/summary    (action = what to summarise/produce)
  file_write   — write file to workspace           (action = filename, content = body; {{prev_result}} to paste prior step output)
  file_read    — read file from workspace          (action = filename)
  code_exec    — execute code                      (action = python3|node|bash, content = code; {{prev_result}} to run prior output as code)
  git_op       — git command in workspace          (action = full git command, e.g. "git clone https://github.com/org/repo")
  github_api   — GitHub REST API call              (action = "METHOD /endpoint", content = JSON body for POST/PATCH)
  market_data  — live prices + technical data      (action = comma-separated symbols, e.g. "AAPL,BTC-USD,TSLA")
  trade_order  — place trade via broker            (action = symbol; trade = order object; ALWAYS requires_intercept=true/FINANCIAL_TRANSACTION)

trade object: {"symbol":"AAPL","side":"buy","qty":"10","type":"market","time_in_force":"day","limit_price":null}

Domain guidance:
  SOFTWARE DEV: git_op → file_write → code_exec → github_api → report
  FREELANCE: web_request → analysis → report → file_write
  TRADING: market_data → analysis → trade_order (always intercepted)

Intercept rules (requires_intercept=true + fill category/reason):
  FINANCIAL_TRANSACTION — any trade_order (mandatory)
  IDENTITY_KYC          — government ID / biometrics required
  SECURITY_BLOCKER      — CAPTCHA, MFA, Cloudflare
  CONTRACT_TOS          — legally-binding agreement
  BUDGET_EXHAUSTION     — would exceed allocated spend

Keep plans to 3–7 steps. {{prev_result}} in action/content is replaced at runtime."""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def _emit(task_id: str, level: str, message: str, **extra):
    entry = {"ts": _now(), "level": level, "message": message, **extra}
    task  = TASKS.get(task_id)
    if task is not None:
        task["log"].append(entry)
    q = LOG_QUEUES.get(task_id)
    if q is not None:
        await q.put(entry)


def _task_workspace(workspace_root: str, task_id: str) -> str:
    ws = os.path.join(workspace_root, task_id)
    os.makedirs(ws, exist_ok=True)
    return ws


def _sub(text: str, prev: str) -> str:
    return text.replace("{{prev_result}}", prev[:6000])


# ── Agent execution loop ─────────────────────────────────────────────────────

async def _run(
    task_id:        str,
    objective:      str,
    domain:         Optional[str],
    http:           httpx.AsyncClient,
    ollama_url:     str,
    ollama_model:   str,
    workspace_root: str,
) -> None:
    task      = TASKS[task_id]
    task["status"] = "running"
    workspace = _task_workspace(workspace_root, task_id)
    task["workspace"] = workspace
    prev_result = ""

    # Read live config at execution time (user may have saved settings since startup)
    github_token  = _get_cfg("GITHUB_TOKEN")
    alpaca_key    = _get_cfg("ALPACA_API_KEY")
    alpaca_secret = _get_cfg("ALPACA_API_SECRET")
    alpaca_url    = _get_cfg("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    async def llm(prompt: str, system: str = "") -> str:
        # Prefer Claude (API → subscription bridge); fall back to local Ollama.
        # Graceful fallback also covers bridge rate-limits, so an autonomous run
        # never stalls on the subscription's usage window.
        try:
            text, _engine = await claude_complete(
                system=system or "You are an autonomous AI agent.",
                user=prompt, http=http, max_tokens=2000,
            )
            if text:
                return text.strip()
        except NoClaudeError:
            pass
        except Exception as exc:
            log.warning("Agent Claude call failed (%s) — using Ollama", exc)
        try:
            payload: dict = {"model": ollama_model, "prompt": prompt, "stream": False}
            if system:
                payload["system"] = system
            r = await http.post(f"{ollama_url}/api/generate", json=payload, timeout=120.0)
            r.raise_for_status()
            return r.json().get("response", "").strip()
        except Exception as exc:
            return f"[LLM unavailable: {exc}]"

    try:
        # ── RECON ────────────────────────────────────────────────────────────
        await _emit(task_id, "RECON", "Auditing environment…")
        tools = []
        for t in ["bash","curl","wget","git","python3","node","npm","pip","jq","docker","nmap","nikto"]:
            r = subprocess.run(["which", t], capture_output=True, text=True)
            if r.returncode == 0:
                tools.append(t)
        cap_parts = [f"tools={','.join(tools) or 'none'}"]
        if github_token: cap_parts.append("github=yes")
        if alpaca_key:   cap_parts.append(f"alpaca={'paper' if 'paper' in alpaca_url else 'live'}")
        await _emit(task_id, "RECON", f"Capabilities: {' | '.join(cap_parts)}")
        task["env"] = {"tools": tools, "workspace": workspace}

        # ── PLAN ─────────────────────────────────────────────────────────────
        await _emit(task_id, "PLAN", "Formulating strategy via LLM…")

        domain_hint = {
            "dev":      "Domain: SOFTWARE DEVELOPMENT & BUG BOUNTIES — prefer git_op, file_write, code_exec, github_api.",
            "freelance":"Domain: FREELANCE TASK EXECUTION — prefer web_request, analysis, report, file_write.",
            "trading":  "Domain: FINANCIAL TRADING — start with market_data, then analysis, end with trade_order (always intercepted).",
        }.get(domain or "", "")

        plan_prompt = (
            f"Objective: {objective}\n\n"
            + (f"{domain_hint}\n\n" if domain_hint else "")
            + f"Available shell tools: {', '.join(tools) or 'standard shell'}\n"
            + f"GitHub: {'configured' if github_token else 'not configured'}\n"
            + f"Alpaca: {'paper mode' if alpaca_key and 'paper' in alpaca_url else 'live mode' if alpaca_key else 'not configured'}\n\n"
            + "Create a 3–7 step plan. Use {{prev_result}} to chain steps. "
            + "For trade_order steps: requires_intercept=true, category=FINANCIAL_TRANSACTION, include trade object."
        )
        raw = await llm(plan_prompt, PLAN_SYSTEM)

        plan: dict | None = None
        try:
            s, e = raw.find("{"), raw.rfind("}") + 1
            if s >= 0 and e > s:
                plan = json.loads(raw[s:e])
        except Exception:
            pass

        if not plan or "steps" not in plan:
            plan = {
                "summary": "Analyse the objective and produce a structured report.",
                "steps": [
                    {"id": 1, "description": "Analyse objective",
                     "action_type": "analysis", "action": objective,
                     "content": None, "trade": None,
                     "requires_intercept": False, "intercept_category": None,
                     "intercept_reason": None},
                    {"id": 2, "description": "Write findings report",
                     "action_type": "report", "action": "Summarise the analysis from {{prev_result}}",
                     "content": None, "trade": None,
                     "requires_intercept": False, "intercept_category": None,
                     "intercept_reason": None},
                ],
            }

        task["plan"] = plan
        await _emit(task_id, "PLAN", plan.get("summary", "Plan ready"), plan=plan)

        # ── EXECUTE ──────────────────────────────────────────────────────────
        steps    = plan.get("steps", [])
        prev_sid = None

        for step in steps:
            if task["status"] == "stopped":
                await _emit(task_id, "STOP", "Task stopped by user.")
                break

            sid   = step.get("id", "?")
            desc  = step.get("description", "")
            atype = step.get("action_type", "analysis")
            act   = _sub(str(step.get("action", "") or ""), prev_result)

            # Force intercept for all trade_order steps (safety net)
            if atype == "trade_order" and not step.get("requires_intercept"):
                trade = step.get("trade") or {}
                step["requires_intercept"]  = True
                step["intercept_category"]  = "FINANCIAL_TRANSACTION"
                step["intercept_reason"]    = (
                    f"Trade order: {trade.get('side','?').upper()} "
                    f"{trade.get('qty','?')} {trade.get('symbol', act)} "
                    f"@ {trade.get('type','market').upper()}"
                )

            if prev_sid is not None:
                await _emit(task_id, "STEP_DONE", f"Step {prev_sid} complete.", step_id=prev_sid)
            prev_sid = sid

            await _emit(task_id, f"STEP {sid}", desc, step_id=sid)
            task["current_step"] = sid

            # ── Human intercept ──────────────────────────────────────────────
            if step.get("requires_intercept"):
                cat    = step.get("intercept_category") or "UNKNOWN"
                reason = step.get("intercept_reason") or "This step requires human authorisation."
                idata  = {
                    "id":          str(uuid.uuid4())[:8],
                    "task_id":     task_id,
                    "step_id":     sid,
                    "category":    cat,
                    "reason":      reason,
                    "step":        step,
                    "trade":       step.get("trade") or {},
                    "alpaca_mode": "paper" if "paper" in alpaca_url else "live" if alpaca_key else "off",
                    "market_ctx":  task.get("_market_ctx", ""),
                    "ts":          _now(),
                }
                task["pending_intercept"] = idata
                task["status"] = "intercepted"
                await _emit(task_id, "INTERCEPT",
                            f"⚠️ HUMAN INTERCEPT — {cat}: {reason}",
                            intercept=idata)

                q = asyncio.Queue()
                INT_QUEUES[task_id] = q
                try:
                    resp = await asyncio.wait_for(q.get(), timeout=600.0)
                except asyncio.TimeoutError:
                    await _emit(task_id, "INTERCEPT", "Intercept timed out (10 min). Skipping step.")
                    task["status"] = "running"
                    task["pending_intercept"] = None
                    INT_QUEUES.pop(task_id, None)
                    continue

                task["status"] = "running"
                task["pending_intercept"] = None
                INT_QUEUES.pop(task_id, None)

                if not resp.get("approved"):
                    notes = resp.get("notes") or "no notes"
                    await _emit(task_id, "REJECTED",
                                f"Step {sid} rejected. Notes: {notes}", step_id=sid)
                    continue
                await _emit(task_id, "APPROVED",
                            f"Step {sid} approved — executing…", step_id=sid)

            # ── Execute step ─────────────────────────────────────────────────
            result = ""
            try:
                if atype == "bash":
                    proc = subprocess.run(
                        act, shell=True, capture_output=True, text=True,
                        timeout=30, cwd=workspace,
                    )
                    stdout = (proc.stdout or "").strip()
                    stderr = (proc.stderr or "").strip()
                    result = stdout or stderr or "(no output)"
                    if proc.returncode != 0 and not stdout:
                        await _emit(task_id, "ERROR", f"Exit {proc.returncode}: {result[:400]}", step_id=sid)
                    else:
                        await _emit(task_id, "OUTPUT", result[:800], step_id=sid)

                elif atype == "web_request":
                    try:
                        rh = await http.get(act, timeout=15.0, follow_redirects=True)
                        result = f"HTTP {rh.status_code} — {len(rh.content):,} bytes from {act}"
                        await _emit(task_id, "OUTPUT", result, step_id=sid)
                    except Exception as exc:
                        result = f"Request failed: {exc}"
                        await _emit(task_id, "ERROR", result, step_id=sid)

                elif atype in ("analysis", "report"):
                    market_ctx = task.get("_market_ctx", "")
                    ctx = (f"\n\nMarket context: {market_ctx}" if market_ctx else "")
                    out = await llm(
                        f"Objective: {objective}{ctx}\n\nTask: {_sub(act, prev_result)}\n\n"
                        "Provide a concise, specific response (under 400 words)."
                    )
                    result = out[:1000]
                    await _emit(task_id, "OUTPUT", result, step_id=sid)

                elif atype == "file_write":
                    fname   = (act or "output.txt").strip().lstrip("/").replace("..", "")
                    content = _sub(str(step.get("content") or "{{prev_result}}"), prev_result)
                    fpath   = os.path.join(workspace, fname)
                    fdir    = os.path.dirname(fpath)
                    if fdir:
                        os.makedirs(fdir, exist_ok=True)
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    result = f"✓ Written {len(content):,} chars → {fname}"
                    await _emit(task_id, "OUTPUT", result, step_id=sid)

                elif atype == "file_read":
                    fname = (act or "").strip().lstrip("/").replace("..", "")
                    try:
                        fpath = os.path.join(workspace, fname)
                        with open(fpath, "r", encoding="utf-8") as fh:
                            content = fh.read(4000)
                        result = content
                        await _emit(task_id, "OUTPUT", f"[{fname}]\n{content[:800]}", step_id=sid)
                    except FileNotFoundError:
                        result = f"File not found: {fname}"
                        await _emit(task_id, "ERROR", result, step_id=sid)

                elif atype == "code_exec":
                    lang   = (act or "python3").strip().lower()
                    code   = _sub(str(step.get("content") or "{{prev_result}}"), prev_result)
                    ext    = {"python3":"py","python":"py","node":"js","bash":"sh"}.get(lang, "txt")
                    interp = {"python3":"python3","python":"python3","node":"node","bash":"bash"}.get(lang, lang)
                    script = os.path.join(workspace, f"__exec_{sid}.{ext}")
                    with open(script, "w", encoding="utf-8") as fh:
                        fh.write(code)
                    proc = subprocess.run(
                        [interp, script], capture_output=True, text=True, timeout=60, cwd=workspace,
                    )
                    stdout = (proc.stdout or "").strip()
                    stderr = (proc.stderr or "").strip()
                    result = stdout or stderr or "(no output)"
                    if proc.returncode != 0 and stderr and not stdout:
                        await _emit(task_id, "ERROR", f"Exit {proc.returncode}: {stderr[:600]}", step_id=sid)
                    else:
                        await _emit(task_id, "OUTPUT", result[:800], step_id=sid, code_output=True)

                elif atype == "git_op":
                    cmd = _sub(act, prev_result)
                    proc = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=120, cwd=workspace,
                    )
                    stdout = (proc.stdout or "").strip()
                    stderr = (proc.stderr or "").strip()
                    result = stdout or stderr or "(no output)"
                    if proc.returncode != 0 and not stdout:
                        await _emit(task_id, "ERROR", f"git exit {proc.returncode}: {result[:400]}", step_id=sid)
                    else:
                        await _emit(task_id, "OUTPUT", result[:600], step_id=sid)

                elif atype == "github_api":
                    if not github_token:
                        result = "GitHub API not configured — add your token in Agent Settings (⚙ button)"
                        await _emit(task_id, "ERROR", result, step_id=sid)
                    else:
                        parts    = act.split(None, 1)
                        method   = parts[0].upper() if parts else "GET"
                        endpoint = parts[1].strip() if len(parts) > 1 else "/"
                        body_raw = _sub(str(step.get("content") or ""), prev_result)
                        try:
                            body = json.loads(body_raw) if body_raw.strip().startswith("{") else None
                        except Exception:
                            body = None
                        ghresp = await http.request(
                            method,
                            f"https://api.github.com{endpoint}",
                            headers={
                                "Authorization":        f"Bearer {github_token}",
                                "Accept":               "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28",
                            },
                            json=body,
                            timeout=30.0,
                        )
                        if ghresp.status_code in (200, 201, 204):
                            data = ghresp.json() if ghresp.content else {}
                            if isinstance(data, list):
                                result = f"HTTP {ghresp.status_code} — {len(data)} items\n"
                                for item in data[:8]:
                                    t = item.get("title") or item.get("name") or str(item)[:80]
                                    result += f"  • #{item.get('number','')} {t}\n"
                            else:
                                keys = ["id","number","html_url","name","full_name","state","title"]
                                pts  = [f"{k}={data[k]}" for k in keys if k in data]
                                result = f"HTTP {ghresp.status_code} — " + (", ".join(pts) or str(data)[:200])
                            await _emit(task_id, "OUTPUT", result[:800], step_id=sid)
                        else:
                            result = f"GitHub API error: HTTP {ghresp.status_code} — {ghresp.text[:200]}"
                            await _emit(task_id, "ERROR", result, step_id=sid)

                elif atype == "market_data":
                    symbols = [s.strip().upper() for s in act.split(",") if s.strip()][:6]
                    lines, ctx_parts = [], []
                    for sym in symbols:
                        try:
                            mr = await http.get(
                                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                                params={"range": "3mo", "interval": "1d"},
                                headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0"},
                                timeout=15.0, follow_redirects=True,
                            )
                            if mr.status_code == 200:
                                j    = mr.json()
                                res  = (j.get("chart", {}).get("result") or [{}])[0]
                                meta = res.get("meta", {})
                                q0   = (res.get("indicators", {}).get("quote") or [{}])[0]
                                cls  = [c for c in (q0.get("close") or []) if c is not None]
                                price = meta.get("regularMarketPrice", cls[-1] if cls else 0)
                                prev  = meta.get("previousClose", price)
                                pct   = ((price - prev) / prev * 100) if prev else 0
                                sma20 = (sum(cls[-20:]) / min(len(cls[-20:]), 20)) if cls else price
                                mom   = ((cls[-1] - cls[-22]) / cls[-22] * 100) if len(cls) > 22 else 0
                                curr  = meta.get("currency", "USD")
                                vs    = "↑" if price >= sma20 else "↓"
                                lines.append(
                                    f"{sym:<12} {curr} {price:>10.2f}  "
                                    f"day {pct:+.2f}%  SMA20 {sma20:.2f} {vs}  3m {mom:+.1f}%"
                                )
                                ctx_parts.append(
                                    f"{sym}=price:{price:.2f},day:{pct:+.2f}%,sma20:{sma20:.2f},3m:{mom:+.1f}%"
                                )
                            else:
                                lines.append(f"{sym:<12}  HTTP {mr.status_code}")
                        except Exception as exc:
                            lines.append(f"{sym:<12}  error: {str(exc)[:60]}")
                    result = "\n".join(lines)
                    task["_market_ctx"] = " | ".join(ctx_parts)
                    await _emit(task_id, "OUTPUT", result, step_id=sid, market_data=True)

                elif atype == "trade_order":
                    if not alpaca_key or not alpaca_secret:
                        result = "Alpaca API not configured — add your keys in Agent Settings (⚙ button)"
                        await _emit(task_id, "ERROR", result, step_id=sid)
                    else:
                        trade = step.get("trade") or {}
                        order_payload = {
                            "symbol":        trade.get("symbol", act.strip().upper()) or act.strip().upper(),
                            "qty":           str(trade.get("qty", "1")),
                            "side":          trade.get("side", "buy").lower(),
                            "type":          trade.get("type", "market").lower(),
                            "time_in_force": trade.get("time_in_force", "day"),
                        }
                        if trade.get("limit_price"):
                            order_payload["limit_price"] = str(trade["limit_price"])
                        tresp = await http.post(
                            f"{alpaca_url}/v2/orders",
                            json=order_payload,
                            headers={
                                "APCA-API-KEY-ID":     alpaca_key,
                                "APCA-API-SECRET-KEY": alpaca_secret,
                            },
                            timeout=30.0,
                        )
                        if tresp.status_code in (200, 201):
                            order = tresp.json()
                            result = (
                                f"✓ Order submitted — ID: {order.get('id','?')} | "
                                f"{order_payload['side'].upper()} {order_payload['qty']} "
                                f"{order_payload['symbol']} @ {order_payload['type'].upper()}"
                            )
                            await _emit(task_id, "OUTPUT", result, step_id=sid)
                        else:
                            result = f"Order rejected: HTTP {tresp.status_code} — {tresp.text[:300]}"
                            await _emit(task_id, "ERROR", result, step_id=sid)
                else:
                    result = f"Unknown action_type: {atype}"
                    await _emit(task_id, "ERROR", result, step_id=sid)

            except subprocess.TimeoutExpired:
                await _emit(task_id, "ERROR", f"Step {sid} timed out.", step_id=sid)
            except Exception as exc:
                await _emit(task_id, "ERROR", f"Step {sid} failed: {exc}", step_id=sid)

            step["result"] = result
            step["status"] = "done"
            prev_result    = result

        # ── DONE ─────────────────────────────────────────────────────────────
        if task["status"] != "stopped":
            if prev_sid is not None:
                await _emit(task_id, "STEP_DONE", f"Step {prev_sid} complete.", step_id=prev_sid)
            task["status"] = "complete"
            await _emit(task_id, "COMPLETE", "✓ All steps finished.")

    except Exception as exc:
        log.exception("agent task %s crashed", task_id)
        task["status"] = "error"
        await _emit(task_id, "ERROR", f"Agent crashed: {exc}")
    finally:
        q = LOG_QUEUES.get(task_id)
        if q:
            await q.put(None)


# ── Router factory ───────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    objective: str
    domain:    Optional[str] = None

class InterceptResponse(BaseModel):
    approved: bool
    notes:    Optional[str] = None

class SettingsUpdateRequest(BaseModel):
    updates: Dict[str, str]  # env-key → new value  (empty string = clear)


def build_router(deps: dict) -> APIRouter:
    http:           httpx.AsyncClient = deps["http"]
    settings                          = deps["settings"]
    ollama_url:     str = settings.OLLAMA_URL
    ollama_model:   str = settings.OLLAMA_MODEL
    workspace_root: str = getattr(settings, "AGENT_WORKSPACE", "/tmp/agent_ws")
    os.makedirs(workspace_root, exist_ok=True)

    router = APIRouter()

    # ── Create task ───────────────────────────────────────────────────────────
    @router.post("/tasks")
    async def create_task(req: TaskRequest):
        task_id = str(uuid.uuid4())[:8]
        TASKS[task_id] = {
            "id":                task_id,
            "objective":         req.objective,
            "domain":            req.domain,
            "status":            "queued",
            "created_at":        _now(),
            "plan":              None,
            "current_step":      None,
            "pending_intercept": None,
            "env":               {},
            "log":               [],
            "_market_ctx":       "",
            "workspace":         "",
        }
        LOG_QUEUES[task_id] = asyncio.Queue()
        asyncio.create_task(
            _run(task_id, req.objective, req.domain,
                 http, ollama_url, ollama_model, workspace_root)
        )
        return {"task_id": task_id, "status": "queued"}

    # ── SSE log stream ────────────────────────────────────────────────────────
    @router.get("/tasks/{task_id}/stream")
    async def stream_task(task_id: str):
        if task_id not in TASKS:
            raise HTTPException(404, "Task not found")
        task = TASKS[task_id]

        async def _gen():
            for entry in list(task["log"]):
                yield f"data: {json.dumps(entry)}\n\n"
            if task["status"] in ("complete", "error", "stopped"):
                yield 'data: {"type":"done"}\n\n'
                return
            q = LOG_QUEUES.get(task_id)
            if not q:
                return
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=25.0)
                    if entry is None:
                        yield 'data: {"type":"done"}\n\n'
                        break
                    yield f"data: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Get task (snapshot) ───────────────────────────────────────────────────
    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        if task_id not in TASKS:
            raise HTTPException(404, "Task not found")
        t = dict(TASKS[task_id])
        t.pop("log", None)
        t.pop("_market_ctx", None)
        return t

    # ── List tasks ────────────────────────────────────────────────────────────
    @router.get("/tasks")
    async def list_tasks():
        out = []
        for t in TASKS.values():
            out.append({
                "id":         t["id"],
                "objective":  t["objective"][:80],
                "domain":     t.get("domain"),
                "status":     t["status"],
                "created_at": t["created_at"],
            })
        return {"tasks": list(reversed(out))}

    # ── Stop task ─────────────────────────────────────────────────────────────
    @router.post("/tasks/{task_id}/stop")
    async def stop_task(task_id: str):
        if task_id not in TASKS:
            raise HTTPException(404, "Task not found")
        TASKS[task_id]["status"] = "stopped"
        q = INT_QUEUES.get(task_id)
        if q:
            await q.put({"approved": False, "notes": "task stopped"})
        return {"status": "stopped"}

    # ── Intercept respond ─────────────────────────────────────────────────────
    @router.post("/tasks/{task_id}/intercept/respond")
    async def respond_intercept(task_id: str, body: InterceptResponse):
        if task_id not in TASKS:
            raise HTTPException(404, "Task not found")
        q = INT_QUEUES.get(task_id)
        if not q:
            raise HTTPException(400, "No pending intercept for this task")
        await q.put({"approved": body.approved, "notes": body.notes})
        return {"status": "received"}

    # ── Settings — GET ────────────────────────────────────────────────────────
    @router.get("/settings")
    async def get_settings():
        items = []
        for s in _SETTINGS_REGISTRY:
            raw = _get_cfg(s["key"], s.get("default", ""))
            is_set = bool(raw)
            if s["kind"] == "password" and is_set:
                display = raw[:4] + "•" * min(len(raw) - 4, 20)
            else:
                display = raw  # show URLs / text fields in full
            items.append({
                "key":     s["key"],
                "group":   s["group"],
                "icon":    s["icon"],
                "label":   s["label"],
                "note":    s["note"],
                "url":     s.get("url"),
                "kind":    s["kind"],
                "default": s.get("default", ""),
                "set":     is_set,
                "display": display,
            })
        return {"settings": items}

    # ── Settings — POST ───────────────────────────────────────────────────────
    @router.post("/settings")
    async def update_settings(req: SettingsUpdateRequest):
        valid_keys = {s["key"] for s in _SETTINGS_REGISTRY}
        changed = []
        for k, v in req.updates.items():
            if k not in valid_keys:
                continue
            v = v.strip()
            if v:
                _AGENT_CONFIG[k] = v
            else:
                _AGENT_CONFIG.pop(k, None)
            changed.append(k)
        _save_config(_AGENT_CONFIG)
        log.info("agent settings updated: %s", changed)
        return {"updated": changed, "ok": True}

    # ── Settings — DELETE single key ──────────────────────────────────────────
    @router.delete("/settings/{key}")
    async def clear_setting(key: str):
        valid_keys = {s["key"] for s in _SETTINGS_REGISTRY}
        if key not in valid_keys:
            raise HTTPException(400, f"Unknown setting key: {key}")
        _AGENT_CONFIG.pop(key, None)
        _save_config(_AGENT_CONFIG)
        return {"cleared": key, "ok": True}

    # ── Capabilities ──────────────────────────────────────────────────────────
    @router.get("/capabilities")
    async def get_capabilities():
        alpaca_key = _get_cfg("ALPACA_API_KEY")
        alpaca_url = _get_cfg("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        return {
            "github":       bool(_get_cfg("GITHUB_TOKEN")),
            "alpaca":       bool(alpaca_key and _get_cfg("ALPACA_API_SECRET")),
            "alpaca_mode":  ("paper" if "paper" in alpaca_url else "live") if alpaca_key else "off",
            "workspace":    workspace_root,
            "ollama_model": ollama_model,
        }

    # ── Status overview ───────────────────────────────────────────────────────
    @router.get("/status")
    async def agent_status():
        running     = [t for t in TASKS.values() if t["status"] == "running"]
        intercepted = [t for t in TASKS.values() if t["status"] == "intercepted"]
        return {
            "running":     len(running),
            "intercepted": len(intercepted),
            "total_tasks": len(TASKS),
            "active_id":   (running or intercepted or [{}])[0].get("id"),
        }

    return router

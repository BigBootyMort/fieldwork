"""
Investigation monitoring + change alerts.

Register a target to monitor; on a schedule (or on demand) we re-run the AI
Orchestrator, persist the fresh graph, and DIFF the new findings against the
last snapshot. Newly-appeared findings (a new breach, sanction, court case,
subdomain, threat pulse, counterparty…) become alerts.

State is JSON-backed so monitors + alerts survive restarts. The scheduler job
is registered alongside Fieldwork's existing monitor cycle.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
import json

log = logging.getLogger("fieldwork.inv_monitor")

_PATH = Path(__file__).parent / "inv_monitors.json"


def _load() -> dict:
    try:
        if _PATH.exists():
            return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"monitors": [], "alerts": []}


def _save(data: dict) -> None:
    try:
        _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Finding-key extraction (stable identifiers per investigation) ───────────

def finding_keys(result: dict) -> list[str]:
    """A flat set of stable keys representing what an investigation found."""
    keys: set[str] = set()
    for tool, res in (result.get("results") or {}).items():
        if not isinstance(res, dict):
            continue
        t = tool.lower()
        try:
            if "hibp" in t:
                for b in res.get("breaches", []) or []:
                    nm = b if isinstance(b, str) else (b.get("Name") or b.get("name", ""))
                    if nm:
                        keys.add(f"breach:{nm}")
            if "sanction" in t or "ofac" in t:
                for h in res.get("hits", []) or []:
                    if h.get("caption"):
                        keys.add(f"sanction:{h['caption']}")
            if "court" in t:
                for c in res.get("cases", []) or []:
                    if c.get("case_name"):
                        keys.add(f"court:{c['case_name']}")
            if "cert" in t or "passive" in t:
                for sd in (res.get("subdomains") or [])[:50]:
                    sv = sd if isinstance(sd, str) else sd.get("name", "")
                    if sv:
                        keys.add(f"subdomain:{sv}")
            if "otx" in t:
                for p in res.get("pulses", []) or []:
                    if p.get("name"):
                        keys.add(f"pulse:{p['name'][:80]}")
                if res.get("malicious"):
                    keys.add("flag:malicious")
            if "etherscan" in t:
                for tx in (res.get("transactions") or [])[:30]:
                    if tx.get("hash"):
                        keys.add(f"tx:{tx['hash']}")
        except Exception:
            pass
    return sorted(keys)


# ── CRUD ────────────────────────────────────────────────────────────────────

def list_monitors() -> list[dict]:
    return _load().get("monitors", [])


def add_monitor(target: str, ttype: str = "auto", interval_h: int = 24) -> dict:
    data = _load()
    for m in data["monitors"]:
        if m["target"].lower() == target.lower():
            return m
    mon = {"id": uuid.uuid4().hex[:12], "target": target, "type": ttype,
           "interval_h": max(1, int(interval_h)), "last_run": 0,
           "keys": [], "created_at": int(time.time())}
    data["monitors"].append(mon)
    _save(data)
    return mon


def remove_monitor(mon_id: str) -> bool:
    data = _load()
    before = len(data["monitors"])
    data["monitors"] = [m for m in data["monitors"] if m["id"] != mon_id]
    _save(data)
    return len(data["monitors"]) < before


def list_alerts(limit: int = 100) -> list[dict]:
    return sorted(_load().get("alerts", []), key=lambda a: a["ts"], reverse=True)[:limit]


# ── Run ───────────────────────────────────────────────────────────────────

async def run_monitor(graph_db, mon: dict) -> dict:
    """Re-run one monitor's investigation, diff, persist alerts. Returns summary."""
    # Imported here to avoid import cycles at module load
    from orchestrator import investigate as _investigate
    from graph_intel import persist_investigation as _persist

    res = await _investigate(mon["target"], mon.get("type", "auto"), graph_db=graph_db)
    if res.get("error"):
        return {"id": mon["id"], "error": res["error"], "new": 0}
    try:
        await _persist(graph_db, res, case_id=None)
    except Exception as exc:
        log.debug("monitor persist failed: %s", exc)

    new_keys = set(finding_keys(res))
    old_keys = set(mon.get("keys", []))
    appeared = sorted(new_keys - old_keys)

    data = _load()
    for m in data["monitors"]:
        if m["id"] == mon["id"]:
            m["keys"] = sorted(new_keys)
            m["last_run"] = int(time.time())
            break
    # First run establishes a baseline (no alerts); later runs alert on deltas
    is_baseline = not old_keys
    if appeared and not is_baseline:
        for k in appeared:
            data["alerts"].append({
                "id": uuid.uuid4().hex[:10], "monitor_id": mon["id"],
                "target": mon["target"], "finding": k, "ts": int(time.time()),
                "seen": False,
            })
    data["alerts"] = data["alerts"][-500:]   # cap
    _save(data)
    return {"id": mon["id"], "target": mon["target"],
            "new": 0 if is_baseline else len(appeared),
            "baseline": is_baseline, "total_keys": len(new_keys)}


async def run_due(graph_db) -> dict:
    """Run every monitor whose interval has elapsed (called by the scheduler)."""
    now = time.time()
    ran, alerts = 0, 0
    for mon in list_monitors():
        if now - mon.get("last_run", 0) >= mon["interval_h"] * 3600:
            try:
                r = await run_monitor(graph_db, mon)
                ran += 1
                alerts += r.get("new", 0)
            except Exception as exc:
                log.warning("monitor %s failed: %s", mon.get("id"), exc)
    if ran:
        log.info("inv_monitor: ran %d monitor(s), %d new alert(s)", ran, alerts)
    return {"ran": ran, "alerts": alerts}

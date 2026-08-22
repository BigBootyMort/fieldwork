"""
Thin FastAPI wrapper around VibeTrading's Python API (VibeTradingLabs/vibetrading).

Research mode only — generate + validate + backtest + built-in templates. No live-exchange
adapters are installed (see the Dockerfile "THE WALL"), so no live-trading path exists.

Endpoints (driven by the shell's Quant module in Phase B):
  GET  /health                     liveness, version, whether an LLM key is present
  GET  /templates                  list built-in strategy templates (no LLM)
  POST /template  {name}           built-in template code (no LLM)
  POST /generate  {prompt, model}  NL -> strategy code (litellm; defaults to a Claude model)
  POST /validate  {code}           static validation of a strategy
  POST /backtest  {code, ...}      run a backtest -> normalized metrics

Signatures confirmed against the installed package (v0.4.0). server.py is volume-mounted, so
tweaks need only a container restart. Everything degrades to {"ok": false, "reason": ...}.
"""
import datetime as _dt
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel, Field

log = logging.getLogger("vibetrading-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="VibeTrading service", version="0.1.0")

_LLM_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY")
# VibeTrading defaults to gpt-4o; we run on the user's Claude key, so default to a Claude
# model (litellm resolves `claude-*` to Anthropic). This id is the one the Fieldwork bridge
# uses successfully with this account's key (see docs/kb/llm-engines.md). Override per-request
# or via the VIBE_MODEL env var.
_MODEL = os.getenv("VIBE_MODEL", "claude-haiku-4-5-20251001")


def _asdict(obj):
    """Coerce a VibeTrading result object/dataclass into JSON-friendly data."""
    if isinstance(obj, (dict, list, str, int, float, bool)) or obj is None:
        return obj
    for attr in ("model_dump", "_asdict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return {k: _asdict(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


@app.get("/health")
def health():
    try:
        import vibetrading
        ver = getattr(vibetrading, "__version__", "unknown")
        return {"ok": True, "version": ver, "llm_key": any(os.getenv(k) for k in _LLM_KEYS),
                "model": _MODEL}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


@app.get("/templates")
def templates():
    try:
        from vibetrading.templates import list_templates
        return {"ok": True, "templates": list(list_templates())}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class TplReq(BaseModel):
    name: str = Field("momentum", max_length=40)
    params: dict | None = None   # override template DEFAULTS (asset, leverage, sma_fast, …)


@app.post("/template")
def template(r: TplReq):
    try:
        from vibetrading.templates import get_template
        mod = get_template(r.name)           # returns a template module with .generate()/.DEFAULTS
        code = mod.generate(**(r.params or {}))
        return {"ok": True, "name": r.name, "code": code, "defaults": getattr(mod, "DEFAULTS", {})}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class GenReq(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=1000)
    model: str | None = None


@app.post("/generate")
def generate(r: GenReq):
    if not any(os.getenv(k) for k in _LLM_KEYS):
        return {"ok": False, "reason": "No LLM key set (ANTHROPIC_API_KEY etc.) — generation needs one"}
    try:
        import vibetrading.strategy as strat
        code = strat.generate(r.prompt, model=(r.model or _MODEL))
        return {"ok": True, "model": r.model or _MODEL,
                "code": code if isinstance(code, str) else _asdict(code)}
    except Exception as exc:
        log.warning("generate failed: %s", exc)
        return {"ok": False, "reason": str(exc)}


class CodeReq(BaseModel):
    code: str = Field(..., min_length=10, max_length=50000)


@app.post("/validate")
def validate(r: CodeReq):
    try:
        import vibetrading.strategy as strat
        return {"ok": True, "result": _asdict(strat.validate(r.code))}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class BtReq(BaseModel):
    code: str = Field(..., min_length=10, max_length=50000)
    interval: str = "1h"
    balance: float = 10000
    exchange: str = "binance"
    start: str | None = None   # YYYY-MM-DD
    end: str | None = None


def _parse_day(s):
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


def _normalize_backtest(res) -> dict:
    """res is a dict from backtest.run. Surface headline metrics + an equity curve if present,
    and keep a trimmed copy of everything else for the UI to grow into."""
    if not isinstance(res, dict):
        return {"raw": _asdict(res)}

    def g(*names):
        for n in names:
            if n in res and res[n] is not None:
                return res[n]
        # some builds nest metrics under a "metrics"/"stats" dict
        for parent in ("metrics", "stats", "summary"):
            d = res.get(parent)
            if isinstance(d, dict):
                for n in names:
                    if n in d and d[n] is not None:
                        return d[n]
        return None

    out = {
        "sharpe":       g("sharpe", "sharpe_ratio"),
        "sortino":      g("sortino", "sortino_ratio"),
        "max_drawdown": g("max_drawdown", "max_dd", "maxDrawdown", "max_drawdown_pct"),
        "win_rate":     g("win_rate", "winrate", "win_rate_pct"),
        "total_return": g("total_return", "return", "total_return_pct", "pnl_pct"),
        "num_trades":   g("num_trades", "trades_count", "n_trades", "total_trades"),
        "final_balance": g("final_balance", "final_equity", "ending_balance"),
    }
    equity = g("equity_curve", "equity", "balance_history")
    if isinstance(equity, list):
        out["equity_curve"] = equity[:500]
    out = {k: v for k, v in out.items() if v is not None}
    out["_keys"] = sorted(res.keys())   # so we can see the real shape during Phase A
    return out


@app.post("/backtest")
def backtest(r: BtReq):
    try:
        import vibetrading.backtest as bt
        res = bt.run(
            r.code,
            interval=r.interval,
            initial_balances={"USDC": r.balance},
            exchange=r.exchange,
            start_time=_parse_day(r.start),
            end_time=_parse_day(r.end),
            mute_strategy_prints=True,
        )
        if res is None:
            return {"ok": False, "reason": "backtest returned no results (no data / no trades?)"}
        return {"ok": True, "metrics": _normalize_backtest(res)}
    except Exception as exc:
        log.warning("backtest failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

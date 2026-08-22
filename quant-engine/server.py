"""
Quant engine wrapper — NautilusTrader backtester for the Runi Shell "Quant" module.

Research mode only: Nautilus BacktestEngine against a SIM/exchange venue in simulation.
No live venue/adapter/exchange key is ever wired in.

  GET  /health                          liveness + versions
  POST /backtest {symbol,interval,...}  ccxt OHLCV -> Nautilus bars -> run -> real metrics.
                                        strategy: builtin "ema_cross" (params) OR generated
                                        {code} that defines build_strategy(instrument_id, bar_type).
  POST /generate {prompt, model}        Claude fills a Nautilus strategy scaffold -> code.
  POST /validate {code}                 compile + contract check (defines build_strategy).
"""
import os
import warnings
from decimal import Decimal

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import ccxt
import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.examples.strategies.ema_cross import EMACross, EMACrossConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

app = FastAPI(title="Quant engine (NautilusTrader)", version="1.0.0")

_MODEL = os.getenv("QUANT_MODEL", "claude-haiku-4-5-20251001")
_START_BAL = 1_000_000.0
# base asset -> TestInstrumentProvider factory (USDT-quoted Binance spot).
_INSTR = {"BTC": "btcusdt_binance", "ETH": "ethusdt_binance"}
# ccxt timeframe -> Nautilus (step, aggregation)
_INTERVAL = {"1m": (1, "MINUTE"), "5m": (5, "MINUTE"), "15m": (15, "MINUTE"),
             "1h": (1, "HOUR"), "4h": (4, "HOUR"), "1d": (1, "DAY")}


# ── data ────────────────────────────────────────────────────────────────────
def _fetch_ohlcv(base: str, interval: str, limit: int) -> pd.DataFrame:
    ex = ccxt.binance({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(f"{base}/USDT", timeframe=interval, limit=min(limit, 1000))
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype("float64")


# ── strategy ────────────────────────────────────────────────────────────────
def _make_strategy(spec: dict, instrument_id, bar_type):
    code = spec.get("code")
    if not code:  # builtin EMA cross
        p = spec.get("params") or {}
        return EMACross(config=EMACrossConfig(
            instrument_id=instrument_id, bar_type=bar_type,
            fast_ema_period=int(p.get("fast", 10)), slow_ema_period=int(p.get("slow", 20)),
            trade_size=Decimal(str(p.get("trade_size", "0.10"))),
        ))
    ns: dict = {}
    exec(compile(code, "<generated-strategy>", "exec"), ns)  # noqa: S102 (research sandbox, no live keys)
    fn = ns.get("build_strategy")
    if not callable(fn):
        raise RuntimeError("generated code must define build_strategy(instrument_id, bar_type)")
    return fn(instrument_id, bar_type)


# ── metrics ─────────────────────────────────────────────────────────────────
def _num(d: dict, key):
    v = d.get(key)
    if v is None or (isinstance(v, float) and v != v):  # drop NaN
        return None
    return v


def _metrics(engine) -> dict:
    pos = engine.trader.generate_positions_report()
    an = engine.portfolio.analyzer
    pnl, ret = {}, {}
    try:
        pnl = an.get_performance_stats_pnls(USDT) or {}
    except Exception:
        pass
    try:
        ret = an.get_performance_stats_returns() or {}
    except Exception:
        pass

    curve = []
    if pos is not None and len(pos) and "realized_pnl" in pos.columns:
        try:
            rows = []
            for _, r in pos.iterrows():
                pnl_val = float(str(r["realized_pnl"]).split()[0])
                rows.append((str(r.get("ts_closed") or r.get("ts_opened")), pnl_val))
            rows.sort()
            eq = _START_BAL
            for tclose, v in rows:
                eq += v
                curve.append({"t": tclose, "equity": round(eq, 2)})
        except Exception:
            curve = []

    return {
        "num_trades":       0 if pos is None else int(len(pos)),
        "total_pnl":        _num(pnl, "PnL (total)"),
        "total_return_pct": _num(pnl, "PnL% (total)"),
        "win_rate":         _num(pnl, "Win Rate"),
        "expectancy":       _num(pnl, "Expectancy"),
        "sharpe":           _num(ret, "Sharpe Ratio (252 days)"),
        "sortino":          _num(ret, "Sortino Ratio (252 days)"),
        "profit_factor":    _num(ret, "Profit Factor"),
        "equity_curve":     curve[:500],
    }


def _run_backtest(base, interval, limit, spec) -> dict:
    df = _fetch_ohlcv(base, interval, limit)
    if df.empty:
        return {"ok": False, "reason": "no market data returned"}
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("QUANT-001"), logging=LoggingConfig(bypass_logging=True)))
    try:
        venue = Venue("BINANCE")
        engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=None, starting_balances=[Money(_START_BAL, USDT)])
        instr = getattr(TestInstrumentProvider, _INSTR[base])()
        engine.add_instrument(instr)
        step, agg = _INTERVAL[interval]
        bar_type = BarType.from_str(f"{instr.id}-{step}-{agg}-LAST-EXTERNAL")
        engine.add_data(BarDataWrangler(bar_type=bar_type, instrument=instr).process(df))
        engine.add_strategy(_make_strategy(spec, instr.id, bar_type))
        engine.run()
        m = _metrics(engine)
        m.update({"ok": True, "symbol": f"{base}/USDT", "interval": interval, "bars": len(df)})
        return m
    finally:
        engine.dispose()


# ── LLM ─────────────────────────────────────────────────────────────────────
_SCAFFOLD_SYSTEM = """You write backtest strategies for NautilusTrader (Python).
Return ONLY Python code — no markdown fences, no prose.

Follow this WORKING template EXACTLY. Change ONLY (a) the indicator(s) created in __init__ and
registered in on_start, and (b) the entry/exit logic in on_bar, to match the user's request.
Keep every import, the config class, build_strategy, and the order helpers verbatim. Use ONLY
the exact import paths shown — do not invent modules.

```
from decimal import Decimal
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.indicators import ExponentialMovingAverage, RelativeStrengthIndex

class GenConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal = Decimal("0.10")

class GenStrategy(Strategy):
    def __init__(self, config):
        super().__init__(config)
        self.rsi = RelativeStrengthIndex(14)          # <-- change indicators here

    def on_start(self):
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar):
        if not self.rsi.initialized:
            return
        flat = self.portfolio.is_flat(self.config.instrument_id)
        if self.rsi.value < 30 and flat:              # <-- change entry/exit logic here
            self._market(OrderSide.BUY)
        elif self.rsi.value > 55 and not flat:
            self.close_all_positions(self.config.instrument_id)

    def _market(self, side):
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id, order_side=side,
            quantity=self.instrument.make_qty(self.config.trade_size))
        self.submit_order(order)

def build_strategy(instrument_id, bar_type):
    return GenStrategy(GenConfig(instrument_id=instrument_id, bar_type=bar_type))
```

Available indicators (import from nautilus_trader.indicators): ExponentialMovingAverage(period),
SimpleMovingAverage(period), RelativeStrengthIndex(period),
MovingAverageConvergenceDivergence(fast, slow), BollingerBands(period, k), AverageTrueRange(period).
Read an indicator's current value with `.value` and guard with `.initialized`."""


def _claude(system: str, user: str, model: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    h = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if key.startswith("sk-ant-oat"):
        h["authorization"] = f"Bearer {key}"
        h["anthropic-beta"] = "oauth-2025-04-20"
    else:
        h["x-api-key"] = key
    payload = {"model": model, "max_tokens": 2500, "system": system,
               "messages": [{"role": "user", "content": user}]}
    r = httpx.post("https://api.anthropic.com/v1/messages", headers=h, json=payload, timeout=120.0)
    r.raise_for_status()
    return (r.json().get("content") or [{}])[0].get("text", "").strip()


# ── endpoints ───────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        import nautilus_trader
        return {"ok": True, "nautilus": nautilus_trader.__version__,
                "pandas": pd.__version__, "model": _MODEL,
                "llm_key": bool(os.getenv("ANTHROPIC_API_KEY"))}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class BtReq(BaseModel):
    symbol: str = Field("BTC", max_length=10)     # base asset: BTC | ETH
    interval: str = Field("1h", max_length=5)
    limit: int = Field(500, ge=50, le=1000)
    code: str | None = None                        # generated strategy; None -> builtin ema_cross
    params: dict | None = None


@app.post("/backtest")
def backtest(r: BtReq):
    base = r.symbol.upper().replace("/USDT", "")
    if base not in _INSTR:
        return {"ok": False, "reason": f"symbol must be one of {list(_INSTR)}"}
    if r.interval not in _INTERVAL:
        return {"ok": False, "reason": f"interval must be one of {list(_INTERVAL)}"}
    try:
        return _run_backtest(base, r.interval, r.limit, {"code": r.code, "params": r.params})
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class GenReq(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=1000)
    model: str | None = None


@app.post("/generate")
def generate(r: GenReq):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "reason": "ANTHROPIC_API_KEY not set"}
    try:
        code = _claude(_SCAFFOLD_SYSTEM, r.prompt, r.model or _MODEL)
        if code.startswith("```"):
            code = code.split("```", 2)[1].removeprefix("python").strip()
        return {"ok": True, "model": r.model or _MODEL, "code": code}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class CodeReq(BaseModel):
    code: str = Field(..., min_length=10, max_length=50000)


@app.post("/validate")
def validate(r: CodeReq):
    try:
        compile(r.code, "<strategy>", "exec")
    except SyntaxError as exc:
        return {"ok": True, "valid": False, "reason": f"SyntaxError: {exc}"}
    ns: dict = {}
    try:
        exec(compile(r.code, "<strategy>", "exec"), ns)  # noqa: S102
    except Exception as exc:
        return {"ok": True, "valid": False, "reason": f"import/exec error: {exc}"}
    if not callable(ns.get("build_strategy")):
        return {"ok": True, "valid": False, "reason": "missing build_strategy(instrument_id, bar_type)"}
    return {"ok": True, "valid": True}

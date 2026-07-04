"""FastAPI routes for the Markets module — Yahoo Finance proxy + AI investment tools."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

log = logging.getLogger("markets")

YAHOO_QUOTE    = "https://query2.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART    = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_CRUMB    = "https://query2.finance.yahoo.com/v1/test/getcrumb"
YAHOO_CONSENT  = "https://fc.yahoo.com"
YAHOO_SEARCH   = "https://query2.finance.yahoo.com/v1/finance/search"

YAHOO_HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "*/*",          # crumb returns text/plain; quote returns JSON
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://finance.yahoo.com/",
}

_http:  Optional[httpx.AsyncClient] = None
_crumb: Optional[str]               = None


# ── Crumb auth (required by Yahoo Finance since ~2023) ──────────────────────

async def _refresh_crumb() -> str:
    """Warm the cookie jar and fetch a Yahoo Finance crumb."""
    global _crumb
    try:
        await _http.get(YAHOO_CONSENT, headers=YAHOO_HDRS,
                        follow_redirects=True, timeout=6.0)
        r = await _http.get(YAHOO_CRUMB, headers=YAHOO_HDRS, timeout=6.0)
        r.raise_for_status()
        _crumb = r.text.strip()
        log.info("Yahoo crumb refreshed: %s…", _crumb[:8] if _crumb else "—")
    except Exception as exc:
        log.warning("Crumb fetch failed (%s) — proceeding without.", exc)
        _crumb = ""
    return _crumb


async def _yget(url: str, params: dict) -> dict:
    """GET to Yahoo Finance; refreshes crumb on 401."""
    global _crumb
    if _crumb is None:          # first call
        await _refresh_crumb()
    p = {**params}
    if _crumb:
        p["crumb"] = _crumb
    r = await _http.get(url, params=p, headers=YAHOO_HDRS, timeout=8.0)
    if r.status_code == 401:
        _crumb = None
        await _refresh_crumb()
        if _crumb:
            p["crumb"] = _crumb
        r = await _http.get(url, params=p, headers=YAHOO_HDRS, timeout=8.0)
    r.raise_for_status()
    return r.json()


# ── Helpers ──────────────────────────────────────────────────────────────────

# ── Range → (yahoo_range, yahoo_interval) map ────────────────────────────────
_RANGE_MAP: dict[str, tuple[str, str]] = {
    "7d":  ("7d",  "1d"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1wk"),
    "6mo": ("6mo", "1wk"),
    "1y":  ("1y",  "1mo"),
    "max": ("max", "1mo"),
}


async def _sparkline_for(symbol: str, range_key: str = "7d") -> list[float]:
    """Historical close prices from Yahoo v8 chart endpoint.

    range_key must be one of: 7d | 1mo | 3mo | 6mo | 1y | max
    Returns a flat list of closing prices (NaN/None values stripped).
    """
    yrange, yinterval = _RANGE_MAP.get(range_key, ("7d", "1d"))
    try:
        data = await _yget(
            YAHOO_CHART.format(symbol=symbol),
            {"range": yrange, "interval": yinterval},
        )
        closes = (
            data.get("chart", {})
                .get("result", [{}])[0]
                .get("indicators", {})
                .get("quote", [{}])[0]
                .get("close", [])
        )
        return [round(c, 4) for c in closes if c is not None]
    except Exception as exc:
        log.debug("sparkline(%s, %s): %s", symbol, range_key, exc)
        return []


# ── Claude API helper ─────────────────────────────────────────────────────────

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


from llm_bridge import claude_complete, NoClaudeError  # shared Claude API + bridge


async def _call_claude(
    prompt: str,
    system: str = "You are a professional investment analyst.",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 2000,
    http_client: httpx.AsyncClient = None,
) -> tuple[str, str]:
    """
    Resolve "Claude" via the shared bridge: Claude API → Claude Code
    subscription bridge. Returns (text, engine) with engine ∈
    {"claude","claude-code"}. Raises NoClaudeError if neither is available
    (callers fall back to Ollama).
    """
    client = http_client or httpx.AsyncClient(timeout=120.0)
    try:
        return await claude_complete(
            system=system, user=prompt, http=client,
            max_tokens=max_tokens, model=model,
        )
    finally:
        if not http_client:
            await client.aclose()


# ── USASpending.gov helpers ───────────────────────────────────────────────────

USASPENDING_API = "https://api.usaspending.gov/api/v2"


async def _search_usaspending(company_name: str, limit: int = 10):
    """
    Search USASpending.gov for federal contracts awarded to a company.
    Returns (list of award dicts, total_value float).
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "filters": {
                    "time_period": [{"start_date": "2022-01-01", "end_date": "2025-12-31"}],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "recipient_search_text": [company_name],
                },
                "fields": [
                    "Award ID", "Recipient Name", "Award Amount",
                    "Awarding Agency", "Start Date", "Award Type",
                    "Description", "Period of Performance Current End Date",
                ],
                "sort": "Award Amount",
                "order": "desc",
                "limit": limit,
                "page": 1,
            }
            r = await client.post(
                f"{USASPENDING_API}/search/spending_by_award/",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            results = r.json().get("results", [])

            contracts = []
            total_value = 0
            for award in results:
                amount = float(award.get("Award Amount") or 0)
                total_value += amount
                contracts.append({
                    "award_id":    award.get("Award ID", ""),
                    "recipient":   award.get("Recipient Name", ""),
                    "amount":      amount,
                    "amount_fmt":  f"${amount/1e6:.1f}M" if amount >= 1e6 else f"${amount/1e3:.0f}K",
                    "agency":      award.get("Awarding Agency", ""),
                    "date":        award.get("Start Date", ""),
                    "end_date":    award.get("Period of Performance Current End Date", ""),
                    "type":        award.get("Award Type", ""),
                    "description": (award.get("Description") or "")[:200],
                })
            return contracts, total_value
    except Exception as exc:
        log.warning("USASpending search failed for %r: %s", company_name, exc)
        return [], 0


async def _get_sector_spending(naics_code: str) -> dict:
    """Get recent spending totals by NAICS sector code."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            payload = {
                "filters": {
                    "time_period": [{"start_date": "2023-01-01", "end_date": "2025-12-31"}],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "naics_codes": [naics_code],
                },
                "category": "awarding_agency",
                "limit": 5,
            }
            r = await client.post(
                f"{USASPENDING_API}/search/spending_by_category/awarding_agency/",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=20.0,
            )
            if r.status_code == 200:
                return r.json()
    except Exception as exc:
        log.debug("sector spending error: %s", exc)
    return {}


# ── FRED macro data helper ────────────────────────────────────────────────────

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_MACRO_SERIES = {
    "fed_funds_rate": "FEDFUNDS",
    "treasury_10y":   "DGS10",
    "treasury_2y":    "DGS2",
    "cpi_yoy":        "CPIAUCSL",
    "unemployment":   "UNRATE",
    "gdp_growth":     "A191RL1Q225SBEA",
}

_macro_cache: dict = {"data": {}, "ts": 0.0}
_MACRO_TTL = 3600  # 1 hour


async def _get_fred_series(series_id: str):
    """Fetch the latest value of a FRED series (CSV, no API key needed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(FRED_BASE, params={"id": series_id})
            r.raise_for_status()
            lines = r.text.strip().split("\n")
            for line in reversed(lines[1:]):
                parts = line.split(",")
                if len(parts) >= 2 and parts[1].strip() not in ("", "."):
                    return float(parts[1].strip())
    except Exception as exc:
        log.debug("FRED %s error: %s", series_id, exc)
    return None


async def _get_macro_context() -> dict:
    """Fetch key macro indicators, cached for 1 hour."""
    global _macro_cache
    if time.time() - _macro_cache["ts"] < _MACRO_TTL and _macro_cache["data"]:
        return _macro_cache["data"]

    results = {}
    for key, series_id in _MACRO_SERIES.items():
        try:
            results[key] = await _get_fred_series(series_id)
        except Exception:
            results[key] = None

    t10 = results.get("treasury_10y")
    t2  = results.get("treasury_2y")
    results["yield_curve"] = round(t10 - t2, 2) if (t10 and t2) else None
    results["inverted"] = (results["yield_curve"] is not None and results["yield_curve"] < 0)

    _macro_cache = {"data": results, "ts": time.time()}
    return results


# ── Research wrapper helpers ──────────────────────────────────────────────────

async def _compute_indicators_for_symbol(symbol: str) -> dict:
    """Wrapper to compute indicators for a symbol, returns dict or {}."""
    try:
        ohlcv = await _fetch_ohlcv(symbol, "1y")
        if not ohlcv["closes"]:
            return {}
        return _compute_indicators(symbol, "1y", ohlcv)
    except Exception as exc:
        log.debug("indicators(%s): %s", symbol, exc)
        return {}


async def _get_single_quote(symbol: str) -> dict:
    """Get a single quote dict for a symbol."""
    try:
        data = await _yget(YAHOO_QUOTE, {
            "symbols": symbol,
            "fields": "shortName,regularMarketPrice,regularMarketChange,regularMarketChangePercent,fiftyTwoWeekHigh,fiftyTwoWeekLow,marketState",
        })
        results = data.get("quoteResponse", {}).get("result", [])
        if results:
            r = results[0]
            return {
                "price":       r.get("regularMarketPrice"),
                "week52_high": r.get("fiftyTwoWeekHigh"),
                "week52_low":  r.get("fiftyTwoWeekLow"),
                "change_pct":  r.get("regularMarketChangePercent"),
            }
    except Exception:
        pass
    return {}


# ── Agent config reader (mirrors agent/routes.py pattern) ────────────────────

from pathlib import Path as _Path
_AGENT_CONFIG_FILE = _Path(__file__).parent.parent / "agent" / "agent_config.json"
_AGENT_CONFIG_CACHE: dict[str, str] = {}

def _load_agent_config() -> dict[str, str]:
    try:
        if _AGENT_CONFIG_FILE.exists():
            return json.loads(_AGENT_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _get_cfg(key: str, default: str = "") -> str:
    """Read from agent_config.json → process env → default."""
    cfg = _load_agent_config()
    return cfg.get(key) or os.environ.get(key) or default


# ── Pure-Python technical indicator helpers ──────────────────────────────────

def _sma(prices: list, period: int) -> list:
    return [
        sum(prices[max(0, i - period + 1):i + 1]) / min(i + 1, period)
        for i in range(len(prices))
    ]


def _ema(prices: list, period: int) -> list:
    k = 2 / (period + 1)
    result = [prices[0]] if prices else []
    for p in prices[1:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list, period: int = 14) -> list:
    if len(closes) < period + 2:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Seed the first real RSI value at index `period`
    rs0 = avg_gain / avg_loss if avg_loss != 0 else 100
    rsi_vals = [50.0] * period + [round(100 - (100 / (1 + rs0)), 2)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        rsi_vals.append(round(100 - (100 / (1 + rs)), 2))
    return rsi_vals


def _macd(closes: list) -> dict:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    line = [round(e12 - e26, 4) for e12, e26 in zip(ema12, ema26)]
    signal = _ema(line, 9)
    hist = [round(l - s, 4) for l, s in zip(line, signal)]
    return {"line": line, "signal": signal, "histogram": hist}


def _bollinger(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
    upper, middle, lower = [], [], []
    for i in range(len(closes)):
        window = closes[max(0, i - period + 1):i + 1]
        m = sum(window) / len(window)
        std = (sum((x - m) ** 2 for x in window) / len(window)) ** 0.5
        middle.append(round(m, 4))
        upper.append(round(m + std_dev * std, 4))
        lower.append(round(m - std_dev * std, 4))
    return {"upper": upper, "middle": middle, "lower": lower}


# ── Risk / return ratios (from the daily close series) ─────────────────────────

def _pct_return(closes: list, days: int) -> float | None:
    """% change over the last `days` trading days."""
    if len(closes) <= days or closes[-days - 1] == 0:
        return None
    return round((closes[-1] / closes[-days - 1] - 1) * 100, 2)


def _daily_returns(closes: list) -> list:
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1]:
            out.append(closes[i] / closes[i - 1] - 1)
    return out


def _annualized_vol(closes: list, window: int = 30) -> float | None:
    """Annualised volatility (%) from the last `window` daily returns."""
    rets = _daily_returns(closes)[-window:]
    if len(rets) < 5:
        return None
    try:
        sd = statistics.pstdev(rets)
    except Exception:
        return None
    return round(sd * (252 ** 0.5) * 100, 2)


def _max_drawdown(closes: list) -> float | None:
    """Largest peak-to-trough decline (%) over the series."""
    if len(closes) < 2:
        return None
    peak, mdd = closes[0], 0.0
    for p in closes:
        if p > peak:
            peak = p
        if peak:
            mdd = min(mdd, p / peak - 1)
    return round(mdd * 100, 2)


def _sharpe(closes: list, rf_annual: float = 0.04) -> float | None:
    """Annualised Sharpe ratio from daily returns (risk-free default 4%)."""
    rets = _daily_returns(closes)
    if len(rets) < 20:
        return None
    try:
        mean, sd = statistics.mean(rets), statistics.pstdev(rets)
    except Exception:
        return None
    if sd == 0:
        return None
    daily_rf = rf_annual / 252
    return round((mean - daily_rf) / sd * (252 ** 0.5), 2)


def _tech_score(rsi: float, macd_bull: bool, above20: bool, above50: bool,
                above200: bool, ret_3m: float | None) -> tuple[int, str]:
    """
    Composite 0-100 technical bullishness score + verdict. Transparent weights:
      trend (SMAs) 45 · momentum (MACD + 3m return) 30 · RSI positioning 25.
    """
    score = 0.0
    score += 15 if above20 else 0
    score += 15 if above50 else 0
    score += 15 if above200 else 0
    score += 18 if macd_bull else 0
    if ret_3m is not None:
        score += 12 if ret_3m > 0 else 0
    # RSI: reward healthy 45-65 zone, penalise extremes lightly
    if 45 <= rsi <= 65:
        score += 25
    elif 35 <= rsi < 45 or 65 < rsi <= 75:
        score += 15
    elif rsi < 30:
        score += 8            # oversold — potential reversal, not confirmation
    else:
        score += 5
    score = int(round(max(0, min(100, score))))
    verdict = ("STRONG BUY" if score >= 78 else "BUY" if score >= 60 else
               "HOLD" if score >= 42 else "SELL" if score >= 25 else "STRONG SELL")
    return score, verdict


async def _fetch_ohlcv(symbol: str, range_key: str = "3mo") -> dict:
    """Fetch full OHLCV from Yahoo chart endpoint.

    Always uses 1d interval so RSI(14), MACD(26), and SMA(200) have
    enough daily data points regardless of the display range.
    We fetch 1y of daily data — ~252 candles — sufficient for all
    indicators, then the caller can slice to the requested range.
    """
    # Always fetch 1y at 1d for indicators — gives ~252 data points,
    # enough for SMA200 and RSI(14) to converge properly.
    yrange, yinterval = "1y", "1d"
    try:
        data = await _yget(
            YAHOO_CHART.format(symbol=symbol),
            {"range": yrange, "interval": yinterval},
        )
        result = data.get("chart", {}).get("result", [{}])[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes  = [c for c in (quote.get("close")  or []) if c is not None]
        volumes = [v for v in (quote.get("volume") or []) if v is not None]
        meta    = result.get("meta", {})
        return {
            "closes":     closes,
            "volumes":    volumes,
            "timestamps": timestamps[:len(closes)],
            "meta":       meta,
        }
    except Exception as exc:
        log.debug("ohlcv(%s, %s): %s", symbol, range_key, exc)
        return {"closes": [], "volumes": [], "timestamps": [], "meta": {}}


def _compute_indicators(symbol: str, range_key: str, ohlcv: dict) -> dict:
    """Compute all indicators from OHLCV dict and return structured response."""
    closes = ohlcv["closes"]
    volumes = ohlcv["volumes"]
    timestamps = ohlcv["timestamps"]
    meta = ohlcv.get("meta", {})

    if not closes:
        raise HTTPException(404, f"No price data for {symbol}")

    current_price = closes[-1]

    sma20_series  = _sma(closes, 20)
    sma50_series  = _sma(closes, 50)
    sma200_series = _sma(closes, 200)
    ema12_series  = _ema(closes, 12)
    ema26_series  = _ema(closes, 26)
    rsi_series    = _rsi(closes, 14)
    macd_data     = _macd(closes)
    bb_data       = _bollinger(closes, 20)
    vol_sma20     = _sma(volumes, 20) if volumes else []

    rsi_current = rsi_series[-1] if rsi_series else 50.0
    if rsi_current < 30:
        rsi_signal = "OVERSOLD"
    elif rsi_current > 70:
        rsi_signal = "OVERBOUGHT"
    else:
        rsi_signal = "NEUTRAL"

    hist = macd_data["histogram"]
    macd_trend = "BULLISH" if hist and hist[-1] > 0 else "BEARISH"

    def _sma_signal(series: list) -> str:
        return "ABOVE" if series and current_price > series[-1] else "BELOW"

    bb_width_pct = 0.0
    if bb_data["upper"] and bb_data["lower"] and bb_data["middle"] and bb_data["middle"][-1]:
        bb_width_pct = round(
            (bb_data["upper"][-1] - bb_data["lower"][-1]) / bb_data["middle"][-1] * 100, 2
        )

    # ── Risk / return ratios ──────────────────────────────────────────────
    ret_1m, ret_3m = _pct_return(closes, 21), _pct_return(closes, 63)
    ret_6m, ret_1y = _pct_return(closes, 126), _pct_return(closes, 252)
    vol30 = _annualized_vol(closes, 30)
    mdd   = _max_drawdown(closes)
    sharpe = _sharpe(closes)
    hi52 = meta.get("fiftyTwoWeekHigh")
    lo52 = meta.get("fiftyTwoWeekLow")
    dist_high = round((current_price / hi52 - 1) * 100, 2) if hi52 else None
    dist_low  = round((current_price / lo52 - 1) * 100, 2) if lo52 else None
    score, verdict = _tech_score(
        rsi_current, hist and hist[-1] > 0,
        _sma_signal(sma20_series) == "ABOVE",
        _sma_signal(sma50_series) == "ABOVE",
        _sma_signal(sma200_series) == "ABOVE", ret_3m,
    )

    return {
        "symbol":     symbol,
        "range":      range_key,
        "closes":     [round(c, 4) for c in closes],
        "timestamps": timestamps,
        "volume":     volumes,
        "sma_20":  {"series": [round(v, 4) for v in sma20_series],  "current": round(sma20_series[-1], 4)  if sma20_series  else None, "signal": _sma_signal(sma20_series)},
        "sma_50":  {"series": [round(v, 4) for v in sma50_series],  "current": round(sma50_series[-1], 4)  if sma50_series  else None, "signal": _sma_signal(sma50_series)},
        "sma_200": {"series": [round(v, 4) for v in sma200_series], "current": round(sma200_series[-1], 4) if sma200_series else None, "signal": _sma_signal(sma200_series)},
        "rsi":     {"series": rsi_series, "current": round(rsi_current, 2), "signal": rsi_signal},
        "macd":    {"line": macd_data["line"], "signal_line": macd_data["signal"], "histogram": hist, "trend": macd_trend},
        "bb":      {"upper": bb_data["upper"], "middle": bb_data["middle"], "lower": bb_data["lower"], "width_pct": bb_width_pct},
        "volume_sma_20": {"series": [round(v, 2) for v in vol_sma20], "current": round(vol_sma20[-1], 2) if vol_sma20 else 0},
        "ratios": {
            "return_1m": ret_1m, "return_3m": ret_3m,
            "return_6m": ret_6m, "return_1y": ret_1y,
            "volatility_30d": vol30, "max_drawdown": mdd, "sharpe": sharpe,
            "dist_52w_high": dist_high, "dist_52w_low": dist_low,
            "tech_score": score, "verdict": verdict,
        },
        "meta":    {"price": meta.get("regularMarketPrice", current_price), "week52_high": meta.get("fiftyTwoWeekHigh"), "week52_low": meta.get("fiftyTwoWeekLow")},
    }


# ── Screener universe + cache ─────────────────────────────────────────────────

_SCREENER_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B", "JPM", "V",
    "UNH",  "XOM",  "LLY",  "JNJ",   "MA",   "HD",   "PG",   "MRK",   "ABBV","CVX",
    "KO",   "PEP",  "COST", "WMT",   "BAC",  "DIS",  "NFLX", "AMD",   "INTC","PLTR",
]

_CRYPTO_UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "ADA-USD",
    "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD", "MATIC-USD", "LTC-USD",
    "TRX-USD", "SHIB-USD", "UNI-USD", "ATOM-USD", "XLM-USD", "NEAR-USD",
]

_OVERVIEW_SYMBOLS = ["^GSPC", "^IXIC", "^DJI", "^VIX", "GC=F", "CL=F", "BTC-USD", "DX-Y.NYB"]

# Separate caches per asset class so stock/crypto results don't clobber each other
_screener_cache: dict = {"stocks": {"data": None, "ts": 0}, "crypto": {"data": None, "ts": 0}}

_SCREENER_RANGE_MAP = {"range": "7d", "interval": "1d"}


# ── Request models (must be at module level for Pydantic v2) ─────────────────

class AnalyzeRequest(BaseModel):
    symbol:       str
    name:         str = ""
    include_news: bool = False
    range:        str = "3mo"


class OrderRequest(BaseModel):
    symbol:        str
    qty:           int
    side:          str
    type:          str = "market"
    limit_price:   Optional[float] = None
    time_in_force: str = "day"


class ResearchRequest(BaseModel):
    symbol:            str
    company_name:      str
    model:             str = "claude-haiku-4-5-20251001"
    include_contracts: bool = True
    include_macro:     bool = True


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _alpaca_headers() -> dict | None:
    key    = _get_cfg("ALPACA_API_KEY")
    secret = _get_cfg("ALPACA_API_SECRET")
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}


def _alpaca_url(path: str) -> str:
    base = _get_cfg("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    return f"{base.rstrip('/')}{path}"


# ── Router factory ───────────────────────────────────────────────────────────

def build_router(deps: dict) -> APIRouter:
    global _http
    # Use the shared async HTTP client from deps (cookie-capable by default)
    _http = deps.get("http") or httpx.AsyncClient(
        timeout=10.0, follow_redirects=True
    )

    router = APIRouter()

    # ── GET /api/markets/quote?symbols=AAPL,TSLA ─────────────────────────
    @router.get("/quote")
    async def quote(
        symbols: str = Query(..., description="Comma-separated tickers, max 20"),
    ):
        """Proxy Yahoo Finance quote endpoint (CORS-blocked in browser)."""
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
        if not syms:
            raise HTTPException(400, "symbols required")
        try:
            data = await _yget(
                YAHOO_QUOTE,
                {
                    "symbols": ",".join(syms),
                    "fields": (
                        "shortName,regularMarketPrice,regularMarketChange,"
                        "regularMarketChangePercent,currency,marketState,"
                        "fiftyTwoWeekHigh,fiftyTwoWeekLow,regularMarketVolume"
                    ),
                },
            )
            results = data.get("quoteResponse", {}).get("result", [])
            out = []
            for r in results:
                out.append({
                    "symbol":      r.get("symbol"),
                    "name":        r.get("shortName") or r.get("symbol"),
                    "price":       r.get("regularMarketPrice"),
                    "change":      r.get("regularMarketChange"),
                    "change_pct":  r.get("regularMarketChangePercent"),
                    "currency":    r.get("currency", "USD"),
                    "market_state": r.get("marketState"),
                    "week52_high": r.get("fiftyTwoWeekHigh"),
                    "week52_low":  r.get("fiftyTwoWeekLow"),
                })
            return {"quotes": out, "count": len(out)}
        except httpx.TimeoutException:
            raise HTTPException(504, "Yahoo Finance timeout")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code,
                                f"Yahoo Finance: {exc.response.text[:200]}")
        except Exception as exc:
            log.exception("quote error")
            raise HTTPException(502, f"Quote fetch failed: {exc}")

    # ── GET /api/markets/search?q=apple ─────────────────────────────────
    @router.get("/search")
    async def search_symbols(q: str = Query(..., min_length=1, max_length=60)):
        """Proxy Yahoo Finance symbol search — CORS-blocked in browser."""
        q = q.strip()
        if not q:
            return {"results": [], "query": q}
        try:
            # Direct GET — search endpoint doesn't require the crumb token
            r = await _http.get(
                YAHOO_SEARCH,
                params={
                    "q":               q,
                    "lang":            "en-US",
                    "region":          "US",
                    "quotesCount":     12,
                    "newsCount":       0,
                    "enableFuzzyQuery":"true",
                    "quotesQueryId":   "tss_match_phrase_query",
                },
                headers=YAHOO_HDRS,
                timeout=6.0,
            )
            quotes = r.json().get("quotes", []) or [] if r.status_code == 200 else []
            results = [
                {
                    "symbol":   item["symbol"],
                    "name":     item.get("shortname") or item.get("longname") or item["symbol"],
                    "type":     item.get("typeDisp", "Equity"),
                    "exchange": item.get("exchDisp", ""),
                }
                for item in quotes
                if item.get("symbol")
            ]
            return {"results": results[:12], "query": q}
        except Exception as exc:
            log.debug("search(%s): %s", q, exc)
            return {"results": [], "query": q}   # non-fatal — frontend falls back to local list

    # ── GET /api/markets/sparklines?symbols=AAPL,TSLA&range=7d ──────────
    @router.get("/sparklines")
    async def sparklines(
        symbols: str = Query(..., description="Comma-separated tickers, max 10"),
        range:   str = Query("7d",  description="7d | 1mo | 3mo | 6mo | 1y | max"),
    ):
        """Return historical daily/weekly close arrays for equity symbols.

        range controls the window — defaults to 7d for backward compat.
        """
        syms      = [s.strip().upper() for s in symbols.split(",") if s.strip()][:10]
        range_key = range if range in _RANGE_MAP else "7d"
        if not syms:
            raise HTTPException(400, "symbols required")
        results = await asyncio.gather(
            *[_sparkline_for(s, range_key) for s in syms], return_exceptions=False
        )
        return {
            "sparklines": {s: d for s, d in zip(syms, results)},
            "range":      range_key,
        }

    # ── GET /api/markets/indicators ──────────────────────────────────────────
    @router.get("/indicators")
    async def indicators(
        symbol: str = Query(..., min_length=1, max_length=20),
        range:  str = Query("3mo", description="1mo | 3mo | 6mo | 1y"),
    ):
        """Compute RSI, MACD, Bollinger Bands, SMAs for a symbol."""
        sym       = symbol.strip().upper()
        range_key = range if range in _RANGE_MAP else "3mo"
        ohlcv     = await _fetch_ohlcv(sym, range_key)
        return _compute_indicators(sym, range_key, ohlcv)

    # ── POST /api/markets/analyze ────────────────────────────────────────────
    @router.post("/analyze")
    async def analyze(req: AnalyzeRequest):
        """AI-powered technical analysis via Ollama."""
        sym       = req.symbol.strip().upper()
        name      = req.name or sym
        range_key = req.range if req.range in _RANGE_MAP else "3mo"

        ohlcv = await _fetch_ohlcv(sym, range_key)
        ind   = _compute_indicators(sym, range_key, ohlcv)

        price      = ind["meta"].get("price", ind["closes"][-1] if ind["closes"] else 0)
        week52_h   = ind["meta"].get("week52_high", "N/A")
        week52_l   = ind["meta"].get("week52_low",  "N/A")
        rsi_cur    = ind["rsi"]["current"]
        rsi_sig    = ind["rsi"]["signal"]
        macd_trend = ind["macd"]["trend"]
        sma20_sig  = ind["sma_20"]["signal"]
        sma50_sig  = ind["sma_50"]["signal"]
        sma200_sig = ind["sma_200"]["signal"]
        bb_width   = ind["bb"]["width_pct"]

        prompt = (
            f"Analyse {sym} ({name}) for a retail investor.\n\n"
            f"Current price: ${price:.2f}\n"
            f"52-week range: ${week52_l} — ${week52_h}\n"
            f"RSI(14): {rsi_cur} ({rsi_sig})\n"
            f"MACD: {macd_trend}\n"
            f"Price vs SMA20: {sma20_sig}, SMA50: {sma50_sig}, SMA200: {sma200_sig}\n"
            f"Bollinger Band width: {bb_width}%\n\n"
            "Write a structured analysis with these exact sections:\n"
            "## Summary (2 sentences — overall technical picture)\n"
            "## Signals (bullet list of key technical signals, each starting with ✅ BULLISH, ⚠️ NEUTRAL, or 🔴 BEARISH)\n"
            "## Key Levels (support and resistance based on the data)\n"
            "## Risk Factors (2-3 specific risks for this trade)\n"
            "## Verdict (one of: STRONG BUY / BUY / HOLD / SELL / STRONG SELL with brief rationale)\n\n"
            "Be specific and data-driven. Do not give generic disclaimers."
        )
        system = (
            "You are a professional financial analyst. Provide clear, actionable technical analysis. "
            "Always include risk warnings. Never guarantee returns."
        )

        settings    = deps.get("settings")
        ollama_url  = getattr(settings, "OLLAMA_URL",   "http://ollama:11434")
        ollama_model= getattr(settings, "OLLAMA_MODEL", "llama3.2")

        try:
            r = await _http.post(
                f"{ollama_url}/api/generate",
                json={"model": ollama_model, "prompt": prompt, "system": system, "stream": False},
                timeout=90.0,
            )
            r.raise_for_status()
            analysis_text = r.json().get("response", "").strip()
        except Exception as exc:
            analysis_text = f"[AI analysis unavailable: {exc}]"

        return {
            "symbol":      sym,
            "analysis":    analysis_text,
            "indicators_summary": {
                "rsi":       rsi_cur,
                "rsi_signal": rsi_sig,
                "macd_trend": macd_trend,
                "sma20_signal": sma20_sig,
                "sma50_signal": sma50_sig,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── GET /api/markets/screener ────────────────────────────────────────────
    async def _screen_one(sym: str) -> dict:
        """Compute screener signals for one symbol using 1y daily data for accurate RSI."""
        try:
            ohlcv = await _fetch_ohlcv(sym, "1y")
            if not ohlcv["closes"]:
                return None
            closes = ohlcv["closes"]
            meta   = ohlcv.get("meta", {})
            price  = meta.get("regularMarketPrice", closes[-1])
            prev   = meta.get("previousClose", price)
            change_pct = ((price - prev) / prev * 100) if prev else 0.0

            rsi_ser  = _rsi(closes, 14)
            macd_d   = _macd(closes)
            sma20    = _sma(closes, 20)
            sma50    = _sma(closes, 50)
            sma200   = _sma(closes, 200)

            rsi_cur    = round(rsi_ser[-1], 2)
            hist       = macd_d["histogram"]
            macd_bull  = bool(hist and hist[-1] > 0)
            macd_trend = "BULLISH" if macd_bull else "BEARISH"
            sma50_sig  = "ABOVE" if sma50 and price > sma50[-1] else "BELOW"
            bullish_macd = len(hist) >= 2 and hist[-2] < 0 and hist[-1] > 0

            ret_3m = _pct_return(closes, 63)
            score, verdict = _tech_score(
                rsi_cur, macd_bull,
                bool(sma20 and price > sma20[-1]),
                bool(sma50 and price > sma50[-1]),
                bool(sma200 and price > sma200[-1]), ret_3m,
            )

            if rsi_cur < 30:
                signal = "oversold"
            elif rsi_cur > 70:
                signal = "overbought"
            elif bullish_macd:
                signal = "bullish_macd"
            elif macd_trend == "BULLISH" and rsi_cur > 50 and sma50_sig == "ABOVE":
                signal = "momentum"
            else:
                signal = "neutral"

            return {
                "symbol":      sym,
                "price":       round(price, 4),
                "change_pct":  round(change_pct, 2),
                "rsi":         rsi_cur,
                "macd_trend":  macd_trend,
                "sma50_signal": sma50_sig,
                "signal":      signal,
                "return_3m":   ret_3m,
                "tech_score":  score,
                "verdict":     verdict,
            }
        except Exception as exc:
            log.debug("screen_one(%s): %s", sym, exc)
            return None

    @router.get("/screener")
    async def screener(filter: str = Query("all"),
                       asset: str = Query("stocks")):
        """Screen liquid stocks or top crypto for technical signals (cached 10 min).

        asset=stocks (30 large caps) | crypto (18 majors). Rows include a
        composite tech_score + verdict; results sort best-score-first.
        """
        asset = "crypto" if asset == "crypto" else "stocks"
        universe = _CRYPTO_UNIVERSE if asset == "crypto" else _SCREENER_UNIVERSE
        cache = _screener_cache[asset]
        now = time.time()
        if cache["data"] and (now - cache["ts"]) < 600:
            results = cache["data"]
        else:
            raw = await asyncio.gather(*[_screen_one(s) for s in universe])
            results = [r for r in raw if r is not None]
            results.sort(key=lambda r: r.get("tech_score", 0), reverse=True)
            cache["data"] = results
            cache["ts"]   = now

        filtered = results if filter == "all" else [r for r in results if r["signal"] == filter]
        return {"results": filtered, "screened": len(results), "filter": filter, "asset": asset}

    # ── GET /api/markets/overview ────────────────────────────────────────────
    @router.get("/overview")
    async def overview():
        """Fetch quotes for key indices and macro symbols."""
        try:
            data = await _yget(
                YAHOO_QUOTE,
                {
                    "symbols": ",".join(_OVERVIEW_SYMBOLS),
                    "fields":  "shortName,regularMarketPrice,regularMarketChange,regularMarketChangePercent",
                },
            )
            results = data.get("quoteResponse", {}).get("result", [])
            out = [
                {
                    "symbol":     r.get("symbol"),
                    "name":       r.get("shortName") or r.get("symbol"),
                    "price":      r.get("regularMarketPrice"),
                    "change":     r.get("regularMarketChange"),
                    "change_pct": r.get("regularMarketChangePercent"),
                }
                for r in results
            ]
        except Exception as exc:
            log.warning("overview fetch failed: %s", exc)
            out = []
        return {"indices": out, "generated_at": datetime.now(timezone.utc).isoformat()}

    # ── GET /api/markets/alpaca/account ─────────────────────────────────────
    @router.get("/alpaca/account")
    async def alpaca_account():
        """Return Alpaca account summary or {configured: false}."""
        hdrs = _alpaca_headers()
        if not hdrs:
            return {"configured": False}
        try:
            r = await _http.get(_alpaca_url("/v2/account"), headers=hdrs, timeout=10.0)
            r.raise_for_status()
            acc = r.json()
            alpaca_base = _get_cfg("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            return {
                "configured":      True,
                "equity":          acc.get("equity"),
                "cash":            acc.get("cash"),
                "buying_power":    acc.get("buying_power"),
                "portfolio_value": acc.get("portfolio_value"),
                "day_trade_count": acc.get("daytrade_count"),
                "mode":            "paper" if "paper" in alpaca_base else "live",
            }
        except Exception as exc:
            raise HTTPException(502, f"Alpaca account fetch failed: {exc}")

    # ── GET /api/markets/alpaca/positions ────────────────────────────────────
    @router.get("/alpaca/positions")
    async def alpaca_positions():
        """Return list of current Alpaca positions."""
        hdrs = _alpaca_headers()
        if not hdrs:
            return {"positions": [], "configured": False}
        try:
            r = await _http.get(_alpaca_url("/v2/positions"), headers=hdrs, timeout=10.0)
            r.raise_for_status()
            raw = r.json()
            positions = [
                {
                    "symbol":          p.get("symbol"),
                    "qty":             p.get("qty"),
                    "market_value":    p.get("market_value"),
                    "unrealized_plpc": p.get("unrealized_plpc"),
                    "current_price":   p.get("current_price"),
                }
                for p in raw
            ]
            return {"positions": positions}
        except Exception as exc:
            raise HTTPException(502, f"Alpaca positions fetch failed: {exc}")

    # ── POST /api/markets/alpaca/order ───────────────────────────────────────
    _order_last_ts: list[float] = [0.0]

    @router.post("/alpaca/order")
    async def alpaca_order(req: OrderRequest):
        """Place an order via Alpaca (rate-limited: 1 per 5 seconds)."""
        now = time.time()
        if now - _order_last_ts[0] < 5:
            raise HTTPException(429, "Rate limit: wait 5 seconds between orders")
        hdrs = _alpaca_headers()
        if not hdrs:
            raise HTTPException(400, "Alpaca not configured")
        payload: dict = {
            "symbol":        req.symbol.strip().upper(),
            "qty":           str(req.qty),
            "side":          req.side.lower(),
            "type":          req.type.lower(),
            "time_in_force": req.time_in_force,
        }
        if req.limit_price is not None:
            payload["limit_price"] = str(req.limit_price)
        try:
            r = await _http.post(_alpaca_url("/v2/orders"), json=payload, headers=hdrs, timeout=30.0)
            if r.status_code not in (200, 201):
                raise HTTPException(r.status_code, f"Alpaca order rejected: {r.text[:300]}")
            _order_last_ts[0] = time.time()
            return r.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Alpaca order failed: {exc}")

    # ── GET /api/markets/research/contracts ──────────────────────────────────
    @router.get("/research/contracts")
    async def get_contracts(
        company: str = Query(..., min_length=2, max_length=100),
        limit:   int = Query(10, ge=1, le=25),
    ):
        """Search USASpending.gov for federal contracts awarded to a company."""
        contracts, total = await _search_usaspending(company, limit)
        return {
            "company":           company,
            "contracts":         contracts,
            "total_value":       total,
            "total_value_fmt":   f"${total/1e9:.2f}B" if total >= 1e9 else f"${total/1e6:.1f}M",
            "count":             len(contracts),
        }

    # ── GET /api/markets/macro ────────────────────────────────────────────────
    @router.get("/macro")
    async def get_macro():
        """Return key FRED macro indicators (1-hour cache)."""
        data = await _get_macro_context()
        return {"macro": data, "cached": bool(_macro_cache["ts"])}

    # ── POST /api/markets/research/analyze ───────────────────────────────────
    @router.post("/research/analyze")
    async def deep_research(req: ResearchRequest):
        """
        Full-depth AI investment research combining technical indicators,
        government contract data (USASpending.gov), FRED macro context,
        and Claude API analysis (falls back to Ollama if no key).
        """
        from datetime import datetime as _dt, timezone as _tz

        settings     = deps.get("settings")
        ollama_url   = getattr(settings, "OLLAMA_URL",   "http://ollama:11434")
        ollama_model = getattr(settings, "OLLAMA_MODEL", "llama3.2")

        # Gather all data concurrently
        ind_task      = asyncio.create_task(_compute_indicators_for_symbol(req.symbol))
        macro_task    = asyncio.create_task(_get_macro_context()) if req.include_macro else None
        contract_task = asyncio.create_task(_search_usaspending(req.company_name, 8)) if req.include_contracts else None
        quote_task    = asyncio.create_task(_get_single_quote(req.symbol))

        ind_result              = await ind_task
        macro_result            = await macro_task if macro_task else {}
        quote_result            = await quote_task
        contracts, total_contracts = await contract_task if contract_task else ([], 0)

        # Build contract summary for prompt
        if contracts:
            contract_lines = "\n".join([
                f"  • {c['amount_fmt']} from {c['agency']} ({c['date'][:7] if c['date'] else 'n/a'}): {c['description'][:120]}"
                for c in contracts[:6]
            ])
            total_fmt = f"${total_contracts/1e9:.2f}B" if total_contracts >= 1e9 else f"${total_contracts/1e6:.1f}M"
            contract_summary = f"Total federal contracts (2022–2025): {total_fmt}\nRecent awards:\n{contract_lines}"
        else:
            contract_summary = "No significant federal contract data found (company may be B2C or data unavailable)."

        # Build macro summary
        m = macro_result or {}
        macro_lines = "\n".join(filter(None, [
            f"  • Fed Funds Rate: {m.get('fed_funds_rate', 'N/A')}%",
            f"  • 10Y Treasury: {m.get('treasury_10y', 'N/A')}%",
            f"  • 2Y Treasury: {m.get('treasury_2y', 'N/A')}%",
            f"  • Yield Curve Spread (10Y-2Y): {m.get('yield_curve', 'N/A')}%"
            + (" ⚠ INVERTED" if m.get("inverted") else ""),
            f"  • CPI (latest): {m.get('cpi_yoy', 'N/A')}",
            f"  • Unemployment: {m.get('unemployment', 'N/A')}%",
        ]))

        # Build technical summary
        price  = quote_result.get("price", "N/A")
        high52 = quote_result.get("week52_high", "N/A")
        low52  = quote_result.get("week52_low",  "N/A")

        ind      = ind_result or {}
        rsi      = ind.get("rsi",    {})
        macd     = ind.get("macd",   {})
        sma20    = ind.get("sma_20",  {})
        sma50    = ind.get("sma_50",  {})
        sma200   = ind.get("sma_200", {})

        tech_lines = "\n".join(filter(None, [
            f"  • Current Price: ${price}",
            f"  • 52-Week Range: ${low52} – ${high52}",
            f"  • RSI(14): {rsi.get('current', 'N/A')} → {rsi.get('signal', 'N/A')}",
            f"  • MACD Trend: {macd.get('trend', 'N/A')}",
            f"  • Price vs SMA20: {sma20.get('signal', 'N/A')}, SMA50: {sma50.get('signal', 'N/A')}, SMA200: {sma200.get('signal', 'N/A')}",
        ]))

        prompt = f"""You are conducting institutional-grade investment research on {req.company_name} ({req.symbol}).

TECHNICAL DATA:
{tech_lines}

GOVERNMENT CONTRACTS (USASpending.gov):
{contract_summary}

MACROECONOMIC CONTEXT (FRED):
{macro_lines}

Produce a comprehensive investment research note with EXACTLY these sections:

## EXECUTIVE SUMMARY
3 sentences covering the overall investment thesis right now.

## GOVERNMENT EXPOSURE ANALYSIS
How significant are federal contracts to this company's revenue? What agencies are buying? Is the contract pipeline growing or contracting? What specific government programs or initiatives drive demand? Be specific about dollar amounts from the data above.

## TECHNICAL PICTURE
Interpret the RSI, MACD, and moving average signals. What pattern do they suggest? Where are key support/resistance levels based on the 52-week range?

## MACRO TAILWINDS & HEADWINDS
How does the current rate environment, yield curve, and inflation picture affect this company specifically? Consider their sector.

## BULL CASE (3 specific catalysts)
Use the contract data and technicals. Be specific, not generic.

## BEAR CASE (3 specific risks)
Be specific. Reference the data.

## PRICE TARGET
6-month price target with a percentage upside/downside from current ${price}. Explain your methodology briefly.

## VERDICT
One of: STRONG BUY | BUY | HOLD | SELL | STRONG SELL
One sentence rationale.

Be data-driven and specific. Reference specific dollar amounts from contracts. Avoid generic disclaimers."""

        system = """You are a senior equity research analyst at a top-tier investment bank.
You write clear, data-driven investment notes. You are direct about your views.
You use specific numbers from the data provided. You do not hedge with excessive disclaimers."""

        analysis_text = None
        model_used    = req.model

        try:
            # Claude API → Claude Code subscription bridge
            analysis_text, engine = await _call_claude(
                prompt, system, req.model, max_tokens=2500, http_client=_http,
            )
            model_used = req.model if engine == "claude" else "claude-code (subscription)"
        except NoClaudeError:
            log.info("No Claude engine available — falling back to Ollama for research")
            try:
                full_prompt = f"{system}\n\n{prompt}"
                r = await _http.post(
                    f"{ollama_url}/api/generate",
                    json={"model": ollama_model, "prompt": full_prompt, "stream": False},
                    timeout=120.0,
                )
                r.raise_for_status()
                analysis_text = r.json().get("response", "")
                model_used    = f"ollama/{ollama_model}"
            except Exception as oe:
                raise HTTPException(502, f"Both Claude and Ollama failed: {oe}")
        except Exception as e:
            raise HTTPException(502, f"Claude error: {e}")

        return {
            "symbol":                 req.symbol,
            "company":                req.company_name,
            "analysis":               analysis_text,
            "model_used":             model_used,
            "contracts":              contracts,
            "total_contracts_value":  total_contracts,
            "macro":                  macro_result,
            "generated_at":           _dt.now(_tz.utc).isoformat(),
        }

    return router

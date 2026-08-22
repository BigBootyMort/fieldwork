"""
GATE: prove NautilusTrader books real trades on BARS — the exact check VibeTrading failed.

Feeds a synthetic oscillating BTCUSDT price (guaranteed EMA crossovers) as external 1h bars
to Nautilus's own EMACross strategy and reports orders filled / positions opened.
PASS iff trades are booked. (Bars mirror the real harness: ccxt OHLCV -> Nautilus Bars.)
"""
from decimal import Decimal

import numpy as np
import pandas as pd

pd.set_option("mode.copy_on_write", False)  # wrangler's Cython needs writable buffers

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.examples.strategies.ema_cross import EMACross, EMACrossConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("GATE-001")))

BINANCE = Venue("BINANCE")
engine.add_venue(
    venue=BINANCE, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
    base_currency=None, starting_balances=[Money(1_000_000, USDT)],
)

BTCUSDT = TestInstrumentProvider.btcusdt_binance()
engine.add_instrument(BTCUSDT)

# Synthetic oscillating price -> repeated fast/slow EMA crossovers -> trades.
n = 600
idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
t = np.arange(n)
close = (100_000 + 5_000 * np.sin(t / 12.0)).astype(np.float64)
open_ = np.empty(n, dtype=np.float64); open_[0] = close[0]; open_[1:] = close[:-1]
high = (np.maximum(open_, close) + 50).astype(np.float64)
low = (np.minimum(open_, close) - 50).astype(np.float64)
vol = np.full(n, 10.0, dtype=np.float64)
df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                  index=idx).copy()

bar_type = BarType.from_str("BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL")
bars = BarDataWrangler(bar_type=bar_type, instrument=BTCUSDT).process(df)
engine.add_data(bars)

cfg = EMACrossConfig(
    instrument_id=BTCUSDT.id, bar_type=bar_type,
    fast_ema_period=10, slow_ema_period=20, trade_size=Decimal("0.10"),
)
engine.add_strategy(EMACross(config=cfg))
engine.run()

fills = engine.trader.generate_order_fills_report()
positions = engine.trader.generate_positions_report()
print("ORDER_FILLS:", len(fills))
print("POSITIONS:", len(positions))
print("GATE:", "PASS - trades booked" if len(positions) > 0 else "FAIL - 0 trades")
engine.dispose()

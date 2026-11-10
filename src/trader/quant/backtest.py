"""Small, transparent bar simulator. One symbol, long/flat, fully invested when long.

Assumptions (all pessimistic or neutral, all stated in reports):
- A signal decided at the close of bar t is executed at the OPEN of bar t+1.
- Each fill pays ``cost_per_side`` (fee + half spread + slippage) as a fraction of notional.
- A stop is fixed at entry: entry - stop_mult * ATR(signal bar). It is checked against the bar LOW from the
  entry bar onward. If the bar opens below the stop, the fill is the open (gap), not the stop.
- After a stop-out, re-entry waits until the signal has gone flat once.
- No partial fills, queue position, latency or market impact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from trader.quant.strategies import StrategySignal


@dataclass
class Trade:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    net_return: float
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    strategy: str
    equity: pd.Series  # indexed by bar position, value at each bar close (starts at 1.0 before bar 0)
    returns: pd.Series  # per-bar net simple returns
    open_time: pd.Series
    in_position: pd.Series
    trades: list[Trade] = field(default_factory=list)
    cost_per_side: float = 0.0


def run_backtest(candles: pd.DataFrame, signal: StrategySignal, cost_per_side: float) -> BacktestResult:
    opens = candles["open"].to_numpy(dtype=float)
    lows = candles["low"].to_numpy(dtype=float)
    closes = candles["close"].to_numpy(dtype=float)
    times = candles["open_time"].to_numpy(dtype=np.int64)
    target = signal.target.to_numpy(dtype=float)
    atr_values = signal.atr.to_numpy(dtype=float)
    n = len(candles)

    equity = np.ones(n)
    held = np.zeros(n)
    trades: list[Trade] = []

    cash = 1.0
    units = 0.0
    entry_price = 0.0
    entry_equity = 0.0
    entry_index = -1
    stop = np.nan
    blocked = False  # after a stop-out, wait for the signal to reset

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, units, stop
        cash = units * price * (1.0 - cost_per_side)
        trades.append(
            Trade(
                int(times[entry_index]),
                int(times[i]),
                entry_price,
                price,
                cash / entry_equity - 1.0,
                i - entry_index,
                reason,
            )
        )
        units = 0.0
        stop = np.nan

    for i in range(n):
        desired = target[i - 1] if i > 0 else 0.0
        if desired == 0.0:
            blocked = False
        if units > 0 and desired == 0.0:
            close_position(i, opens[i], "signal")
        elif units == 0 and desired == 1.0 and not blocked:
            entry_equity = cash
            entry_price = opens[i]
            units = cash * (1.0 - cost_per_side) / entry_price
            entry_index = i
            prior_atr = atr_values[i - 1]
            stop = (
                entry_price - signal.stop_mult * prior_atr
                if signal.stop_mult > 0 and np.isfinite(prior_atr)
                else np.nan
            )
        if units > 0 and np.isfinite(stop) and lows[i] <= stop:
            close_position(i, min(opens[i], stop), "stop")
            blocked = True
        held[i] = 1.0 if units > 0 else 0.0
        equity[i] = units * closes[i] if units > 0 else cash

    if units > 0:  # mark the open trade at the last close without charging an exit that did not happen
        trades.append(
            Trade(
                int(times[entry_index]),
                int(times[-1]),
                entry_price,
                float(closes[-1]),
                equity[-1] / entry_equity - 1.0,
                n - 1 - entry_index,
                "open_at_end",
            )
        )

    equity_series = pd.Series(equity)
    returns = equity_series.pct_change()
    returns.iloc[0] = equity[0] - 1.0
    return BacktestResult(
        signal.name,
        equity_series,
        returns,
        candles["open_time"].reset_index(drop=True),
        pd.Series(held),
        trades,
        cost_per_side,
    )

"""Baseline strategies. Long/flat only. Each returns a target position decided at the bar close.

These are hypotheses under test, not validated edges (research/strategy-landscape.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trader.quant.features import atr, ema


@dataclass(frozen=True)
class StrategySignal:
    """target: 1.0 = long, 0.0 = flat, decided at close of each bar (NaN warm-up -> 0).
    atr: ATR at that close, used for the stop. stop_mult: stop distance in ATRs."""

    name: str
    version: str
    target: pd.Series
    atr: pd.Series
    stop_mult: float
    params: dict[str, float]
    detail: pd.DataFrame


def tsmom_trend_v1(candles: pd.DataFrame, params: dict[str, float] | None = None) -> StrategySignal:
    p = {"ema_fast": 24, "ema_slow": 168, "mom_lookback": 168, "atr_stop_mult": 3.0, **(params or {})}
    close = candles["close"]
    fast, slow = ema(close, int(p["ema_fast"])), ema(close, int(p["ema_slow"]))
    mom = pd.Series(np.log(close / close.shift(int(p["mom_lookback"]))), index=close.index)
    target = ((fast > slow) & (mom > 0)).astype(float).where(slow.notna() & mom.notna(), 0.0)
    detail = pd.DataFrame({"ema_fast": fast, "ema_slow": slow, "momentum": mom})
    return StrategySignal(
        "tsmom_trend_v1",
        "1",
        target,
        atr(candles["high"], candles["low"], close, 14),
        float(p["atr_stop_mult"]),
        p,
        detail,
    )


def donchian_breakout_v1(candles: pd.DataFrame, params: dict[str, float] | None = None) -> StrategySignal:
    p = {"entry_lookback": 96, "exit_lookback": 48, "atr_stop_mult": 3.0, **(params or {})}
    close = candles["close"]
    upper = candles["high"].rolling(int(p["entry_lookback"])).max().shift(1)
    lower = candles["low"].rolling(int(p["exit_lookback"])).min().shift(1)
    enter = (close > upper).to_numpy()
    leave = (close < lower).to_numpy()
    valid = (upper.notna() & lower.notna()).to_numpy()
    state = np.zeros(len(candles))
    holding = 0.0
    for i in range(len(candles)):
        if not valid[i]:
            holding = 0.0
        elif holding == 0.0 and enter[i]:
            holding = 1.0
        elif holding == 1.0 and leave[i]:
            holding = 0.0
        state[i] = holding
    target = pd.Series(state, index=candles.index)
    detail = pd.DataFrame({"entry_level": upper, "exit_level": lower})
    return StrategySignal(
        "donchian_breakout_v1",
        "1",
        target,
        atr(candles["high"], candles["low"], close, 14),
        float(p["atr_stop_mult"]),
        p,
        detail,
    )


def buy_and_hold(candles: pd.DataFrame, params: dict[str, float] | None = None) -> StrategySignal:
    target = pd.Series(1.0, index=candles.index)
    never = pd.Series(np.nan, index=candles.index)
    return StrategySignal("buy_and_hold", "1", target, never, 0.0, {}, pd.DataFrame(index=candles.index))


def no_trade(candles: pd.DataFrame, params: dict[str, float] | None = None) -> StrategySignal:
    target = pd.Series(0.0, index=candles.index)
    never = pd.Series(np.nan, index=candles.index)
    return StrategySignal("no_trade", "1", target, never, 0.0, {}, pd.DataFrame(index=candles.index))


STRATEGIES: dict[str, Callable[[pd.DataFrame, dict[str, float] | None], StrategySignal]] = {
    "tsmom_trend_v1": tsmom_trend_v1,
    "donchian_breakout_v1": donchian_breakout_v1,
    "buy_and_hold": buy_and_hold,
    "no_trade": no_trade,
}
ACTIVE_STRATEGIES = ["tsmom_trend_v1", "donchian_breakout_v1"]

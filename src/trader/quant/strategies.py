"""Baseline strategies. Long/flat only. Each returns a target position decided at the bar close.

These are hypotheses under test, not validated edges (research/strategy-landscape.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trader.quant.features import atr, ema
from trader.timeutil import bars_per_year, interval_ms


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


def volatility_scaled_signal(
    candles: pd.DataFrame,
    base: StrategySignal,
    interval: str,
    target_volatility: pd.Series,
    lookback_bars: int = 24 * 30,
) -> StrategySignal:
    """Scale a long/flat signal by trailing realized volatility, capped at 100% exposure.

    ``target_volatility`` must be calibrated from data available before each evaluation fold.
    A closed-bar estimate at t only affects the position at the open of t+1.
    """
    if lookback_bars < 2 or len(target_volatility) != len(candles):
        raise ValueError("invalid volatility window or target_volatility length")
    close = pd.Series(candles["close"].to_numpy(dtype=float), index=candles.index)
    log_returns = pd.Series(np.log(close / close.shift(1)), index=candles.index)
    realized_vol = log_returns.rolling(lookback_bars, min_periods=lookback_bars).std() * np.sqrt(
        bars_per_year(interval)
    )
    target_volatility = target_volatility.astype(float).reset_index(drop=True)
    rv = realized_vol.reset_index(drop=True)
    base_target = base.target.astype(float).reset_index(drop=True)
    valid = rv.notna() & target_volatility.notna() & (rv > 0)
    multiplier = pd.Series(0.0, index=base_target.index)
    multiplier.loc[valid] = (target_volatility.loc[valid] / rv.loc[valid]).clip(0.0, 1.0)
    target = base_target.clip(0.0, 1.0) * multiplier
    return StrategySignal(
        f"vol_scaled_{base.name}",
        "1",
        target,
        base.atr.reset_index(drop=True),
        base.stop_mult,
        {**base.params, "vol_lookback_bars": float(lookback_bars)},
        pd.DataFrame({"base_target": base_target, "realized_volatility": rv, "target_volatility": target_volatility}),
    )


def daily_rebalance_volatility_signal(
    candles: pd.DataFrame,
    scaled: StrategySignal,
    interval: str,
) -> StrategySignal:
    """Hold the volatility multiplier between UTC-day boundaries; preserve hourly signal exits/entries."""
    if len(candles) != len(scaled.target):
        raise ValueError("signal and candle lengths must match")
    day_ms = 86_400_000
    close_times = candles["open_time"].to_numpy(dtype=np.int64) + interval_ms(interval)
    day_keys = close_times // day_ms
    scheduled = np.r_[True, day_keys[1:] != day_keys[:-1]] if len(day_keys) else np.array([], dtype=bool)
    base_target = scaled.detail["base_target"].to_numpy(dtype=float)
    realized_vol = scaled.detail["realized_volatility"].to_numpy(dtype=float)
    target_vol = scaled.detail["target_volatility"].to_numpy(dtype=float)
    raw_scale = np.zeros(len(candles), dtype=float)
    valid = np.isfinite(realized_vol) & np.isfinite(target_vol) & (realized_vol > 0)
    raw_scale[valid] = np.clip(target_vol[valid] / realized_vol[valid], 0.0, 1.0)

    carried_scale = np.zeros(len(candles), dtype=float)
    current_scale = 0.0
    for i in range(len(candles)):
        if scheduled[i] and valid[i]:
            current_scale = raw_scale[i]
        carried_scale[i] = current_scale
    target = pd.Series(base_target * carried_scale, index=scaled.target.index)
    detail = scaled.detail.copy()
    detail["daily_scale"] = carried_scale
    detail["rebalance_scheduled"] = scheduled
    return StrategySignal(
        f"{scaled.name}_daily",
        scaled.version,
        target,
        scaled.atr,
        scaled.stop_mult,
        {**scaled.params, "rebalance_interval_days": 1.0},
        detail,
    )


STRATEGIES: dict[str, Callable[[pd.DataFrame, dict[str, float] | None], StrategySignal]] = {
    "tsmom_trend_v1": tsmom_trend_v1,
    "donchian_breakout_v1": donchian_breakout_v1,
    "buy_and_hold": buy_and_hold,
    "no_trade": no_trade,
}
ACTIVE_STRATEGIES = ["tsmom_trend_v1", "donchian_breakout_v1"]

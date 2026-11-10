"""Causal indicators and the feature pipeline.

Every function uses trailing windows only, on closed bars. ``tests/unit/test_features.py`` checks that a
feature value at bar t does not change when later bars are appended (no look-ahead).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trader.timeutil import bars_per_year


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    role: str
    formula: str
    lookback_param: str
    units: str
    null_handling: str = "NaN until the lookback is filled; rows with NaN are not used for signals"


FEATURE_REGISTRY: list[FeatureSpec] = [
    FeatureSpec(
        "ema_spread",
        "trend direction",
        "EMA(close, ema_fast) / EMA(close, ema_slow) - 1",
        "ema_slow",
        "fraction",
    ),
    FeatureSpec("tsmom", "trend direction", "ln(close_t / close_{t-n})", "tsmom_lookback", "log return"),
    FeatureSpec("adx", "trend strength", "Wilder ADX", "adx_period", "0-100"),
    FeatureSpec("rsi", "momentum/oscillation", "Wilder RSI", "rsi_period", "0-100"),
    FeatureSpec("atr", "volatility level", "Wilder ATR of true range", "atr_period", "quote currency"),
    FeatureSpec("atr_pct", "volatility level", "ATR / close", "atr_period", "fraction"),
    FeatureSpec(
        "rv", "volatility level", "std(log returns, rv_short) * sqrt(bars per year)", "rv_short", "annualized"
    ),
    FeatureSpec("rv_ratio", "volatility regime", "rv(rv_short) / rv(rv_long)", "rv_long", "ratio"),
    FeatureSpec(
        "rv_pct",
        "volatility regime",
        "trailing percentile rank of rv over vol_regime_window",
        "vol_regime_window",
        "0-1",
    ),
    FeatureSpec(
        "donchian_pos",
        "range location",
        "(close - min(low, n)) / (max(high, n) - min(low, n))",
        "donchian_lookback",
        "0-1",
    ),
    FeatureSpec("rel_volume", "participation", "volume / mean(volume, n)", "volume_lookback", "ratio"),
    FeatureSpec(
        "taker_buy_ratio", "flow proxy", "sum(taker_buy_base, 24) / sum(volume, 24)", "rv_short", "0-1"
    ),
    FeatureSpec(
        "vwap_dev",
        "anchored value",
        "close / (sum(quote_volume, 24) / sum(volume, 24)) - 1",
        "rv_short",
        "fraction",
    ),
]

FEATURE_NAMES = [spec.name for spec in FEATURE_REGISTRY]

DEFAULT_PARAMS: dict[str, int] = {
    "ema_fast": 24,
    "ema_slow": 168,
    "tsmom_lookback": 168,
    "atr_period": 14,
    "adx_period": 14,
    "rsi_period": 14,
    "rv_short": 24,
    "rv_long": 168,
    "donchian_lookback": 96,
    "volume_lookback": 168,
    "vol_regime_window": 2160,
}


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return wilder(true_range(high, low, close), period)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0.0), period)
    loss = wilder((-delta).clip(lower=0.0), period)
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.where(loss != 0.0, 100.0).where(gain.notna())


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr_ = atr(high, low, close, period).replace(0.0, np.nan)
    plus_di = 100.0 * wilder(plus_dm, period) / atr_
    minus_di = 100.0 * wilder(minus_dm, period) / atr_
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return wilder(dx, period)


def compute_features(
    candles: pd.DataFrame, interval: str, params: dict[str, int] | None = None
) -> pd.DataFrame:
    """Return one row per candle: open_time, close_time, close and the registered features."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    c, h, low, v = candles["close"], candles["high"], candles["low"], candles["volume"]
    log_ret = pd.Series(np.log(c / c.shift(1)), index=c.index)
    annual = np.sqrt(bars_per_year(interval))

    out = pd.DataFrame(
        {"open_time": candles["open_time"], "close_time": candles["close_time"], "close": c},
        index=candles.index,
    )
    out["ema_spread"] = ema(c, p["ema_fast"]) / ema(c, p["ema_slow"]) - 1.0
    out["tsmom"] = np.log(c / c.shift(p["tsmom_lookback"]))
    out["adx"] = adx(h, low, c, p["adx_period"])
    out["rsi"] = rsi(c, p["rsi_period"])
    out["atr"] = atr(h, low, c, p["atr_period"])
    out["atr_pct"] = out["atr"] / c
    rv_short = log_ret.rolling(p["rv_short"]).std() * annual
    rv_long = log_ret.rolling(p["rv_long"]).std() * annual
    out["rv"] = rv_short
    out["rv_ratio"] = rv_short / rv_long.replace(0.0, np.nan)
    window = p["vol_regime_window"]
    out["rv_pct"] = rv_short.rolling(window, min_periods=max(p["rv_long"], window // 4)).rank(pct=True)
    hi = h.rolling(p["donchian_lookback"]).max()
    lo = low.rolling(p["donchian_lookback"]).min()
    out["donchian_pos"] = (c - lo) / (hi - lo).replace(0.0, np.nan)
    out["rel_volume"] = v / v.rolling(p["volume_lookback"]).mean().replace(0.0, np.nan)
    vol_sum = v.rolling(p["rv_short"]).sum().replace(0.0, np.nan)
    out["taker_buy_ratio"] = candles["taker_buy_base"].rolling(p["rv_short"]).sum() / vol_sum
    out["vwap_dev"] = c / (candles["quote_volume"].rolling(p["rv_short"]).sum() / vol_sum) - 1.0
    return out


REGIME_METHOD = "rule_regime_v1"
ADX_TREND_THRESHOLD = 20.0  # conventional, provisional
VOL_LOW_PCT, VOL_HIGH_PCT = 1.0 / 3.0, 2.0 / 3.0


def classify_regime(row: pd.Series) -> dict[str, str]:
    """Rule regime from one feature row: trend state x volatility state. 'unknown' when inputs are missing."""
    spread, strength, pct = row.get("ema_spread"), row.get("adx"), row.get("rv_pct")
    if pd.isna(spread) or pd.isna(strength):
        trend = "unknown"
    elif strength >= ADX_TREND_THRESHOLD and spread > 0:
        trend = "trend_up"
    elif strength >= ADX_TREND_THRESHOLD and spread < 0:
        trend = "trend_down"
    else:
        trend = "range"
    if pd.isna(pct):
        vol = "unknown"
    elif pct < VOL_LOW_PCT:
        vol = "vol_low"
    elif pct > VOL_HIGH_PCT:
        vol = "vol_high"
    else:
        vol = "vol_normal"
    return {"trend": trend, "volatility": vol, "label": f"{trend}/{vol}", "method": REGIME_METHOD}


def regime_series(features: pd.DataFrame) -> pd.Series:
    return features.apply(lambda row: classify_regime(row)["label"], axis=1)

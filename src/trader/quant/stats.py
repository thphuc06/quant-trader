"""Performance metrics and overfitting statistics.

PSR / DSR: Bailey & López de Prado (2012, 2014). PBO via CSCV: Bailey, Borwein, López de Prado, Zhu (2017).
Formulas implemented from the published definitions; see research/source-register.md for what was and
was not re-read.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

EULER_GAMMA = 0.5772156649015329


def sharpe_per_period(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 0 else float("nan")


def probabilistic_sharpe_ratio(returns: np.ndarray, benchmark_sr: float = 0.0) -> float:
    """P(true per-period Sharpe > benchmark) given sample length, skew and kurtosis."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    sr = sharpe_per_period(r)
    if n < 30 or not math.isfinite(sr):
        return float("nan")
    g3 = float(skew(r))
    g4 = float(kurtosis(r, fisher=False))
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    return float(norm.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom)))


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Expected maximum per-period Sharpe among n independent trials with true Sharpe zero."""
    if n_trials < 2 or sharpe_variance <= 0:
        return 0.0
    return math.sqrt(sharpe_variance) * (
        (1.0 - EULER_GAMMA) * norm.ppf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(returns: np.ndarray, n_trials: int, sharpe_variance: float) -> float:
    """PSR against the expected best-of-N Sharpe. With n_trials < 2 this equals PSR(0)."""
    return probabilistic_sharpe_ratio(returns, expected_max_sharpe(n_trials, sharpe_variance))


def block_bootstrap_ci(
    returns: np.ndarray,
    statistic: str = "sharpe",
    block: int = 24,
    n_boot: int = 1000,
    ci: float = 0.90,
    seed: int = 7,
) -> tuple[float, float]:
    """Circular block bootstrap confidence interval for the per-period Sharpe or the total return."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < block * 4:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    offsets = np.arange(block)
    values = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n, size=n_blocks)
        idx = ((starts[:, None] + offsets[None, :]) % n).ravel()[:n]
        sample = r[idx]
        if statistic == "sharpe":
            sd = sample.std(ddof=1)
            values[b] = sample.mean() / sd if sd > 0 else 0.0
        else:
            values[b] = np.prod(1.0 + sample) - 1.0
    lo, hi = np.quantile(values, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return float(lo), float(hi)


def pbo_cscv(returns_matrix: np.ndarray, n_splits: int = 10) -> dict[str, Any]:
    """Probability of backtest overfitting.

    returns_matrix: T x N per-period returns of N configurations. Returns PBO = share of train/test
    combinations where the in-sample best configuration ranks at or below the out-of-sample median.
    """
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 4 or n_splits % 2 or m.shape[0] < n_splits * 20:
        return {
            "status": "not_applicable",
            "reason": "needs >=4 configurations, an even split count and >=20 rows per split",
        }
    t, n = m.shape
    blocks = np.array_split(np.arange(t), n_splits)
    logits: list[float] = []

    def sharpe_columns(x: np.ndarray) -> np.ndarray:
        sd = x.std(axis=0, ddof=1)
        return np.where(sd > 0, x.mean(axis=0) / np.where(sd > 0, sd, 1.0), 0.0)

    for combo in itertools.combinations(range(n_splits), n_splits // 2):
        train_idx = np.concatenate([blocks[i] for i in combo])
        test_idx = np.concatenate([blocks[i] for i in range(n_splits) if i not in combo])
        best = int(np.argmax(sharpe_columns(m[train_idx])))
        oos = sharpe_columns(m[test_idx])
        rank = float((oos < oos[best]).sum() + 0.5 * (oos == oos[best]).sum() + 0.5)  # 1..N mid-rank
        omega = min(max(rank / (n + 1.0), 1e-9), 1 - 1e-9)
        logits.append(math.log(omega / (1.0 - omega)))
    arr = np.array(logits)
    return {"status": "ok", "pbo": float((arr <= 0).mean()), "combinations": len(arr), "configurations": n}


def performance_metrics(
    returns: pd.Series, in_position: pd.Series, trades: list[Any], periods_per_year: float
) -> dict[str, Any]:
    r = returns.fillna(0.0).to_numpy(dtype=float)
    n = len(r)
    equity = np.cumprod(1.0 + r)
    total = float(equity[-1] - 1.0) if n else 0.0
    years = n / periods_per_year if periods_per_year else 0.0
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    downside = r[r < 0]
    dd_sd = float(np.sqrt((downside**2).sum() / n)) if n else 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    drawdown = equity / peak - 1.0
    max_dd = float(np.min(drawdown)) if n else 0.0
    underwater = drawdown < 0
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    cagr = float((1.0 + total) ** (1.0 / years) - 1.0) if years >= 1.0 and total > -1.0 else None
    sharpe = float(r.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else None
    sortino = float(r.mean() / dd_sd * math.sqrt(periods_per_year)) if dd_sd > 0 else None

    closed = [t for t in trades if t.exit_reason != "open_at_end"]
    wins = [t.net_return for t in closed if t.net_return > 0]
    losses = [t.net_return for t in closed if t.net_return <= 0]
    streak = worst_streak = 0
    for t in closed:
        streak = streak + 1 if t.net_return <= 0 else 0
        worst_streak = max(worst_streak, streak)
    gross_win, gross_loss = sum(wins), -sum(losses)
    bars_per_day = periods_per_year / 365.0
    daily = (
        pd.Series(r)
        .groupby(np.arange(n) // max(1, round(bars_per_day)))
        .apply(lambda x: float(np.prod(1 + x) - 1))
        if n
        else pd.Series(dtype=float)
    )
    return {
        "bars": n,
        "years": round(years, 2),
        "net_total_return": total,
        "cagr": cagr,
        "annualized_volatility": sd * math.sqrt(periods_per_year) if sd else None,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "max_drawdown_duration_bars": int(longest),
        "calmar": (cagr / abs(max_dd)) if cagr is not None and max_dd < 0 else None,
        "worst_day": float(daily.min()) if len(daily) else None,
        "exposure": float(in_position.mean()) if len(in_position) else 0.0,
        "trades": len(closed),
        "trades_per_year": len(closed) / years if years > 0 else None,
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "payoff_ratio": (float(np.mean(wins)) / abs(float(np.mean(losses))))
        if wins and losses and np.mean(losses) != 0
        else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy_per_trade": float(np.mean([t.net_return for t in closed])) if closed else None,
        "avg_bars_held": float(np.mean([t.bars_held for t in closed])) if closed else None,
        "max_consecutive_losses": worst_streak,
    }

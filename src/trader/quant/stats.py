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


DAILY_BOOTSTRAP_BLOCKS = (5, 10, 20)


def daily_bootstrap_sensitivity(
    daily_returns: np.ndarray,
    blocks: tuple[int, ...] = DAILY_BOOTSTRAP_BLOCKS,
    n_boot: int = 2000,
    ci: float = 0.90,
    days_per_year: float = 365.0,
    seed: int = 7,
) -> dict[str, Any]:
    """Circular block bootstrap on DAILY returns at several block lengths, reported side by side.

    Crypto trades every day, so annualization uses 365. The block length is a modelling choice (how long serial
    dependence lasts); showing 5/10/20-day blocks together makes that choice visible. ``most_conservative`` takes
    the lowest lower bound across block lengths, which is what a gate should use.
    """
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    scale = math.sqrt(days_per_year)
    by_block: dict[str, dict[str, Any]] = {}
    for block in blocks:
        s_lo, s_hi = block_bootstrap_ci(r, "sharpe", block=block, n_boot=n_boot, ci=ci, seed=seed)
        t_lo, t_hi = block_bootstrap_ci(r, "total_return", block=block, n_boot=n_boot, ci=ci, seed=seed)
        by_block[f"{block}d"] = {
            "block_days": block,
            "sharpe_ci_annualized": [s_lo * scale, s_hi * scale],
            "total_return_ci": [t_lo, t_hi],
        }
    lows = [v["sharpe_ci_annualized"][0] for v in by_block.values() if math.isfinite(v["sharpe_ci_annualized"][0])]
    point = sharpe_per_period(r)
    return {
        "days": len(r),
        "ci_level": ci,
        "n_boot": n_boot,
        "sharpe_annualized": point * scale if math.isfinite(point) else float("nan"),
        "total_return": float(np.prod(1.0 + r) - 1.0) if len(r) else 0.0,
        "by_block": by_block,
        "most_conservative_sharpe_lower": min(lows) if len(lows) == len(by_block) else float("nan"),
    }


def paired_sharpe_difference_ci(
    baseline: np.ndarray,
    challenger: np.ndarray,
    periods_per_year: float,
    block: int = 24,
    n_boot: int = 1000,
    ci: float = 0.90,
    seed: int = 7,
) -> tuple[float, float]:
    """Paired circular-block CI for annualized challenger minus baseline Sharpe."""
    a, b = np.asarray(baseline, dtype=float), np.asarray(challenger, dtype=float)
    if a.shape != b.shape:
        raise ValueError("paired return arrays must have the same shape")
    valid = np.isfinite(a) & np.isfinite(b)
    a, b = a[valid], b[valid]
    n = len(a)
    if block < 1 or n < block * 4:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    offsets = np.arange(block)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, size=n_blocks)
        idx = ((starts[:, None] + offsets[None, :]) % n).ravel()[:n]
        sa, sb = a[idx], b[idx]
        sda, sdb = sa.std(ddof=1), sb.std(ddof=1)
        sha = sa.mean() / sda if sda > 0 else 0.0
        shb = sb.mean() / sdb if sdb > 0 else 0.0
        diffs[i] = (shb - sha) * math.sqrt(periods_per_year)
    lo, hi = np.quantile(diffs, [(1 - ci) / 2, 1 - (1 - ci) / 2])
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
    returns: pd.Series,
    in_position: pd.Series,
    trades: list[Any],
    periods_per_year: float,
    turnover: pd.Series | None = None,
    turnover_by_reason: pd.DataFrame | None = None,
    modeled_costs_by_component_and_reason: pd.DataFrame | None = None,
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
    daily_cvar_95 = None
    if len(daily):
        daily_tail = daily[daily <= daily.quantile(0.05)]
        daily_cvar_95 = float(daily_tail.mean()) if len(daily_tail) else None
    max_recovery = 0
    running_peak = 1.0
    running_peak_idx = -1
    underwater_peak_idx: int | None = None
    for i, value in enumerate(equity):
        if value >= running_peak:
            if underwater_peak_idx is not None:
                max_recovery = max(max_recovery, i - underwater_peak_idx)
                underwater_peak_idx = None
            running_peak, running_peak_idx = float(value), i
        elif underwater_peak_idx is None:
            underwater_peak_idx = running_peak_idx
    if underwater_peak_idx is not None:
        max_recovery = max(max_recovery, len(equity) - 1 - underwater_peak_idx)
    result: dict[str, Any] = {
        "bars": n,
        "years": round(years, 2),
        "net_total_return": total,
        "cagr": cagr,
        "annualized_volatility": sd * math.sqrt(periods_per_year) if sd else None,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "max_drawdown_duration_bars": int(longest),
        "max_drawdown_recovery_bars": int(max_recovery),
        "calmar": (cagr / abs(max_dd)) if cagr is not None and max_dd < 0 else None,
        "worst_day": float(daily.min()) if len(daily) else None,
        "daily_cvar_95": daily_cvar_95,
        "exposure": float(in_position.mean()) if len(in_position) else 0.0,
        "turnover_per_year": (
            float(turnover.fillna(0.0).sum() / years) if turnover is not None and years > 0 else None
        ),
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
    if turnover_by_reason is not None and years > 0:
        reason_totals = turnover_by_reason.fillna(0.0).sum(axis=0)
        result["turnover_per_year_by_reason"] = {
            str(reason): float(value / years) for reason, value in reason_totals.items()
        }
    if modeled_costs_by_component_and_reason is not None:
        cost_totals: dict[str, dict[str, float]] = {}
        for column, value in modeled_costs_by_component_and_reason.fillna(0.0).sum(axis=0).items():
            component, reason = str(column).split("__", maxsplit=1)
            cost_totals.setdefault(reason, {})[component] = float(value * 10_000)
        for components in cost_totals.values():
            components["total"] = float(sum(components.values()))
        component_names = sorted(
            {component for values in cost_totals.values() for component in values if component != "total"}
        )
        totals = {
            component: float(
                sum(values.get(component, 0.0) for reason, values in cost_totals.items() if reason != "total")
            )
            for component in component_names
        }
        totals["total"] = float(sum(totals.values()))
        cost_totals["total"] = totals
        result["modeled_cost_bps_initial_equity_by_reason"] = cost_totals
    return result

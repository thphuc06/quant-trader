"""Gates and tiers of research/validation-framework-v2.md, applied to a daily portfolio backtest.

The thresholds come from ``configs/app.yaml`` (``validation_v2``); the definitions are fixed here and documented in
the framework file. Nothing in this module decides which data or parameters to use: protocols do that.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from trader.quant.portfolio import PortfolioResult, daily_returns, portfolio_summary
from trader.quant.stats import (
    daily_bootstrap_sensitivity,
    deflated_sharpe_ratio,
    sharpe_per_period,
    timing_skill_test,
)

FRAMEWORK_VERSION = "v2"
DEFAULTS: dict[str, Any] = {
    "min_trades": 30,
    "timing_p": 0.10,
    "timing_shifts": 2000,
    "timing_margin_days": 30,
    "timing_seed": 7,
    "ci": 0.90,
    "min_dsr": 0.5,
    "bootstrap_blocks": [5, 10, 20],
    "n_boot": 2000,
}


def implied_sharpe_threshold(days: int, ci: float = 0.90, variance_factor: float = 1.15) -> float:
    """Observed annual Sharpe at which a one-sided (1+ci)/2 lower bound reaches zero (normal approximation)."""
    from scipy.stats import norm

    years = days / 365.0
    return float(norm.ppf(1 - (1 - ci) / 2) * np.sqrt(variance_factor / years)) if years > 0 else float("nan")


def concentration(result: PortfolioResult) -> dict[str, Any]:
    closed = sorted((t.net_pnl for t in result.trades if t.exit_reason != "open_at_end"), reverse=True)
    total = float(sum(closed))
    top = closed[: max(1, len(closed) // 5)] if closed else []
    return {
        "closed_trades": len(closed),
        "net_pnl_initial_equity": total,
        "net_pnl_without_two_best": float(sum(closed[2:])) if len(closed) > 2 else None,
        "top_20pct_share_of_gross_profit": (
            float(sum(p for p in top if p > 0) / sum(p for p in closed if p > 0))
            if any(p > 0 for p in closed)
            else None
        ),
        "win_rate": (sum(p > 0 for p in closed) / len(closed)) if closed else None,
    }


def evaluate(
    primary: PortfolioResult,
    stressed: PortfolioResult,
    *,
    per_side_cost_primary: float,
    binding_n: int,
    binding_sharpes: list[float],
    raw_n: int,
    raw_sharpes: list[float],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply G-timing, G-cost, G-ci, G-dsr and G-span to one candidate. ``stressed`` is the 2x-cost run
    (it may be the same object as ``primary`` when the protocol's primary cost level is 2x)."""
    p = {**DEFAULTS, **(params or {})}
    if primary.interval != "1d":
        raise ValueError("framework v2 is defined on daily bars")
    daily = daily_returns(primary).to_numpy(dtype=float)
    summary = portfolio_summary(primary)
    boot = daily_bootstrap_sensitivity(
        daily, tuple(p["bootstrap_blocks"]), n_boot=int(p["n_boot"]), ci=float(p["ci"])
    )
    sharpe_daily = sharpe_per_period(daily)
    summary["sharpe_per_period"] = (
        sharpe_daily  # daily bars: per period = per day (read by the trial registry)
    )

    def dsr(n: int, sharpes: list[float]) -> float:
        finite = [s for s in sharpes if np.isfinite(s)]
        variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
        return deflated_sharpe_ratio(daily, n, variance)

    exposure = primary.symbol_exposure.shift(1).fillna(0.0).to_numpy(dtype=float)
    timing = timing_skill_test(
        exposure,
        primary.asset_returns.to_numpy(dtype=float),
        primary.bar_cost.to_numpy(dtype=float),
        per_side_cost_primary,
        n_shifts=int(p["timing_shifts"]),
        margin=int(p["timing_margin_days"]),
        seed=int(p["timing_seed"]),
    )
    dsr_binding, dsr_raw = dsr(binding_n, binding_sharpes), dsr(raw_n, raw_sharpes)
    stressed_return = float(stressed.equity[-1] - 1.0)
    lower = boot["most_conservative_sharpe_lower"]
    gates = {
        "G-timing": bool(np.isfinite(timing["p_value"]) and timing["p_value"] <= float(p["timing_p"])),
        "G-cost": stressed_return > 0,
        "G-ci": bool(np.isfinite(lower) and lower > 0),
        "G-dsr": bool(np.isfinite(dsr_binding) and dsr_binding >= float(p["min_dsr"])),
        "G-span": summary["trades"] >= int(p["min_trades"]),
    }
    if not (gates["G-timing"] and gates["G-cost"]) or not (np.isfinite(sharpe_daily) and sharpe_daily > 0):
        tier = "REJECTED"
    elif all(gates.values()):
        tier = "VALIDATED"
    else:
        tier = "INCONCLUSIVE"
    return {
        "framework": FRAMEWORK_VERSION,
        "tier": tier,
        "gates": gates,
        "portfolio": summary,
        "net_total_return_at_2x_costs": stressed_return,
        "daily_bootstrap": boot,
        "implied_observed_sharpe_threshold": implied_sharpe_threshold(len(daily), float(p["ci"])),
        "timing_skill": timing,
        "selection_bias": {
            "binding_n": binding_n,
            "dsr_binding": dsr_binding,
            "raw_n": raw_n,
            "dsr_raw_registry": dsr_raw,
            "daily_sharpe_per_day": sharpe_daily,
        },
        "concentration": concentration(primary),
        "daily_series": {
            "close_dates": [str(d.date()) for d in daily_returns(primary).index],
            "net_return": [round(float(x), 10) for x in daily],
            "exposure_by_symbol": {
                str(s): [round(float(x), 8) for x in primary.symbol_exposure[s]]
                for s in primary.symbol_exposure
            },
        },
        "params": p,
    }

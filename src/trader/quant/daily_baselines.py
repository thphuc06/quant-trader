"""Runner for the preregistered protocol research/protocols/daily-baselines-v1.md.

Everything that the protocol fixes is a module constant here; the CLI only supplies data and configuration.
Bars at or after the holdout start are dropped before any computation.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trader.config import CostConfig, RiskPolicy
from trader.quant.portfolio import (
    PortfolioLimits,
    PortfolioResult,
    daily_returns,
    portfolio_summary,
    run_portfolio_backtest,
)
from trader.quant.research import (
    STANDARD_LIMITATIONS,
    ExperimentRegistry,
    asdict_costs,
    code_commit,
    cost_component_rates,
    holdout_start_ms,
    mark_supersedes,
    min_evaluation_trades,
    working_tree_dirty,
)
from trader.quant.stats import daily_bootstrap_sensitivity, deflated_sharpe_ratio, sharpe_per_period
from trader.quant.strategies import STRATEGIES
from trader.timeutil import from_ms, iso, to_ms

PROTOCOL_PATH = "research/protocols/daily-baselines-v1.md"
EVALUATION_START = "2023-11-12T00:00:00+00:00"
SYMBOL_PRIORITY = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
DAILY_PARAMS: dict[str, dict[str, float]] = {
    "tsmom_trend_v1": {"ema_fast": 20, "ema_slow": 100, "mom_lookback": 90, "atr_stop_mult": 3.0},
    "donchian_breakout_v1": {"entry_lookback": 55, "exit_lookback": 20, "atr_stop_mult": 3.0},
}
TURNOVER_CUT_MAX_RATIO = 0.5
BOOTSTRAP_BLOCKS = (5, 10, 20)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def resample_daily(hourly: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Complete UTC days only (24 hourly bars). Incomplete days are dropped and reported, never filled."""
    frame = hourly.drop_duplicates("open_time").sort_values("open_time")
    day = frame["open_time"].to_numpy(dtype=np.int64) // DAY_MS * DAY_MS
    grouped = frame.assign(day=day).groupby("day", sort=True)
    counts = grouped.size()
    agg = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        quote_volume=("quote_volume", "sum"),
        trades=("trades", "sum"),
        taker_buy_base=("taker_buy_base", "sum"),
        taker_buy_quote=("taker_buy_quote", "sum"),
    )
    complete = counts == 24
    daily = agg[complete].reset_index().rename(columns={"day": "open_time"})
    daily["close_time"] = daily["open_time"] + DAY_MS - 1
    incomplete = [iso(from_ms(int(d))) for d in counts.index[~complete]]
    return daily.reset_index(drop=True), {
        "complete_days": int(complete.sum()),
        "incomplete_days_dropped": incomplete,
    }


def compare_with_native(daily: pd.DataFrame, native: pd.DataFrame, before_ms: int) -> dict[str, Any]:
    """Data check against stored native 1d klines on overlapping development days (prices only)."""
    if native.empty:
        return {"status": "no_native_daily_klines"}
    left = daily[daily["open_time"] < before_ms].set_index("open_time")
    right = native[native["open_time"] < before_ms].drop_duplicates("open_time").set_index("open_time")
    common = left.index.intersection(right.index)
    if not len(common):
        return {"status": "no_overlap"}
    cols = ["open", "high", "low", "close"]
    rel = (left.loc[common, cols] - right.loc[common, cols]).abs() / right.loc[common, cols]
    worst = float(rel.to_numpy().max())
    return {
        "status": "ok" if worst < 1e-9 else "mismatch",
        "days_compared": len(common),
        "max_relative_diff": worst,
    }


def _evaluate(result: PortfolioResult, ci: float, n_boot: int) -> dict[str, Any]:
    summary = portfolio_summary(result)
    daily = daily_returns(result).to_numpy(dtype=float)
    summary["daily_bootstrap"] = daily_bootstrap_sensitivity(daily, BOOTSTRAP_BLOCKS, n_boot=n_boot, ci=ci)
    summary["sharpe_per_period"] = sharpe_per_period(result.returns.to_numpy(dtype=float))
    summary["daily_sharpe_per_day"] = sharpe_per_period(daily)
    return summary


def evaluate_daily_baselines(
    hourly: dict[str, pd.DataFrame],
    *,
    policy: RiskPolicy,
    costs: CostConfig,
    hourly_params: dict[str, dict[str, float]],
    holdout_start: str,
    validation: dict[str, float],
    registry: ExperimentRegistry,
    root: Path,
    native_daily: dict[str, pd.DataFrame] | None = None,
    data_quality: dict[str, Any] | None = None,
    n_boot: int = 2000,
    evaluation_start: str = EVALUATION_START,
    record: bool = True,
    supersedes: dict[str, str] | None = None,
    correction_defect: str = "",
) -> list[dict[str, Any]]:
    """Run the frozen protocol once: 2 daily candidates (trials) and 2 hourly references (not trials)."""
    if sorted(hourly) != sorted(SYMBOL_PRIORITY):
        raise ValueError(f"protocol universe is {SYMBOL_PRIORITY}")
    cutoff = holdout_start_ms(holdout_start)
    start_ms = to_ms(pd.Timestamp(evaluation_start).to_pydatetime())
    if start_ms % DAY_MS:
        raise ValueError("evaluation start must be a UTC day boundary")
    dev_hourly = {s: f[f["open_time"] < cutoff].reset_index(drop=True) for s, f in hourly.items()}
    if any(int(f["open_time"].max()) >= cutoff for f in dev_hourly.values()):
        raise RuntimeError("holdout bars reached the evaluation")
    daily: dict[str, pd.DataFrame] = {}
    aggregation: dict[str, Any] = {}
    for symbol, frame in dev_hourly.items():
        daily[symbol], aggregation[symbol] = resample_daily(frame)
        if native_daily is not None:
            aggregation[symbol]["native_1d_check"] = compare_with_native(
                daily[symbol], native_daily.get(symbol, pd.DataFrame(columns=["open_time"])), cutoff
            )

    limits = PortfolioLimits.from_policy(policy)
    ci = float(validation.get("bootstrap_ci", 0.90))
    stress = float(validation.get("cost_stress_multiplier", 2.0))
    base_costs, stress_costs = cost_component_rates(costs), cost_component_rates(costs, stress)
    commit, dirty = code_commit(root), working_tree_dirty(root)

    def simulate(
        data: dict[str, pd.DataFrame], signals: dict[str, Any], interval: str, rates: dict[str, float]
    ) -> PortfolioResult:
        return run_portfolio_backtest(
            data,
            signals,
            interval=interval,
            limits=limits,
            cost_components=rates,
            start_time_ms=start_ms,
            end_time_ms=cutoff,
            priority=SYMBOL_PRIORITY,
            clip_stop_to_policy=True,
        )

    runs: list[dict[str, Any]] = []
    candidate_daily_returns: list[np.ndarray] = []
    for strategy, params in DAILY_PARAMS.items():
        daily_signals = {s: STRATEGIES[strategy](daily[s], params) for s in SYMBOL_PRIORITY}
        hourly_signals = {
            s: STRATEGIES[strategy](dev_hourly[s], hourly_params.get(strategy)) for s in SYMBOL_PRIORITY
        }
        cand = simulate(daily, daily_signals, "1d", base_costs)
        cand_stress = simulate(daily, daily_signals, "1d", stress_costs)
        ref = simulate(dev_hourly, hourly_signals, "1h", base_costs)
        cand_metrics, ref_metrics = _evaluate(cand, ci, n_boot), _evaluate(ref, ci, n_boot)
        stress_metrics = _evaluate(cand_stress, ci, n_boot)
        ratio = (
            cand_metrics["turnover_per_year"] / ref_metrics["turnover_per_year"]
            if ref_metrics["turnover_per_year"]
            else math.inf
        )
        base_payload = {
            "protocol": PROTOCOL_PATH,
            "symbols": SYMBOL_PRIORITY,
            "strategy": strategy,
            "code_commit": commit,
            "working_tree_dirty": dirty,
            "cost_model_bps_per_side": asdict_costs(costs),
            "risk_policy": {"version": policy.version, "status": policy.status, **limits.__dict__},
            "data": {
                "source": "stored Binance Spot 1h candles; daily = complete UTC days aggregated from them",
                "evaluation_start": iso(from_ms(start_ms)),
                "evaluation_end_exclusive": holdout_start,
                "holdout_start": holdout_start,
                "aggregation": aggregation,
                "data_quality": data_quality or {"status": "not_provided"},
            },
            "holdout": {"status": "locked"},
        }
        runs.append(
            {
                **base_payload,
                "role": "reference",
                "counts_toward_dsr": False,
                "interval": "1h",
                "params": hourly_params.get(strategy, {}),
                "hypothesis": f"reference: {strategy} on 1h bars through the same portfolio backtester",
                "portfolio": ref_metrics,
            }
        )
        runs.append(
            {
                **base_payload,
                "role": "candidate",
                "interval": "1d",
                "params": params,
                "hypothesis": f"{strategy} on UTC-daily bars halves portfolio turnover vs 1h and passes all gates",
                "portfolio": cand_metrics,
                "portfolio_at_stressed_costs": {"multiplier": stress, **stress_metrics},
                "turnover_vs_hourly_reference": {
                    "candidate_turnover_per_year": cand_metrics["turnover_per_year"],
                    "reference_turnover_per_year": ref_metrics["turnover_per_year"],
                    "ratio": ratio,
                    "candidate_cost_bps_per_year": cand_metrics["modeled_cost_bps_initial_equity_per_year"],
                    "reference_cost_bps_per_year": ref_metrics["modeled_cost_bps_initial_equity_per_year"],
                },
            }
        )
        candidate_daily_returns.append(daily_returns(cand).to_numpy(dtype=float))

    # DSR for the predeclared pair, counting every earlier trial plus both candidates (Sharpe in daily units).
    candidates = [r for r in runs if r["role"] == "candidate"]
    replaced = frozenset((supersedes or {}).values())
    historical = registry.trial_sharpes(target_interval="1d", exclude=replaced)
    family = [float(r["portfolio"]["daily_sharpe_per_day"]) for r in candidates]
    all_sharpes = [s for s in [*historical, *family] if np.isfinite(s)]
    n_trials = registry.trial_count(exclude=replaced) + len(candidates)
    variance = float(np.var(all_sharpes, ddof=1)) if len(all_sharpes) >= 2 else 0.0
    min_trades = min_evaluation_trades(validation)
    for run, returns in zip(candidates, candidate_daily_returns, strict=True):
        dsr = deflated_sharpe_ratio(returns, n_trials, variance)
        metrics = run["portfolio"]
        lower = metrics["daily_bootstrap"]["most_conservative_sharpe_lower"]
        gates = {
            "turnover_cut_at_least_half": bool(
                run["turnover_vs_hourly_reference"]["ratio"] <= TURNOVER_CUT_MAX_RATIO
            ),
            "enough_trades": metrics["trades"] >= min_trades,
            "sharpe_ci_above_zero": bool(np.isfinite(lower) and lower > 0),
            "dsr_at_least_min": bool(np.isfinite(dsr) and dsr >= float(validation.get("min_dsr", 0.5))),
            "positive_at_stressed_costs": run["portfolio_at_stressed_costs"]["net_total_return"] > 0,
        }
        run["selection_bias"] = {
            "trials_including_this": n_trials,
            "sharpe_variance_across_trials_daily_units": variance,
            "deflated_sharpe_ratio": dsr,
            "note": "earlier trials' per-bar Sharpe rescaled to daily units; raw count treats trials as independent",
        }
        run["trade_count_gate"] = {
            "min_evaluation_trades": min_trades,
            "trades": metrics["trades"],
            "period": "development OOS window 2023-11-12 to 2025-11-10 (exclusive), all four symbols combined",
        }
        run["gates_provisional"] = gates
        run["all_gates_passed"] = all(gates.values())
        run["exploratory_only_oos_previously_viewed"] = True
        run["limitations"] = [
            *[x for x in STANDARD_LIMITATIONS if "Single-symbol" not in x],
            "Daily bars aggregated from 1h candles; incomplete UTC days are dropped, so lookbacks span an extra day near a gap.",
            "Positions are not resized after entry; the fixed BTC>ETH>SOL>BNB tie-break favours earlier symbols.",
            "Stops wider than the policy's 10% maximum are placed at 10% (declared in the protocol).",
            "The development OOS has been viewed by many earlier trials; this result is exploratory, not confirmatory.",
        ]
    mark_supersedes(runs, supersedes, correction_defect)
    if record:
        for run in runs:
            run["experiment_id"] = registry.record("portfolio_backtest", run)
    return runs


def compact(run: dict[str, Any]) -> dict[str, Any]:
    """Short view of one run for the CLI and the research log."""
    p = run["portfolio"]
    out = {
        "experiment_id": run.get("experiment_id"),
        "role": run["role"],
        "strategy": run["strategy"],
        "interval": run["interval"],
        "net_total_return": p["net_total_return"],
        "max_drawdown": p["max_drawdown"],
        "sharpe": p["sharpe"],
        "trades": p["trades"],
        "turnover_per_year": p["turnover_per_year"],
        "cost_bps_per_year": p["modeled_cost_bps_initial_equity_per_year"],
        "mean_gross_exposure": p["mean_gross_exposure"],
        "sharpe_ci_by_block": {
            k: v["sharpe_ci_annualized"] for k, v in p["daily_bootstrap"]["by_block"].items()
        },
    }
    if run["role"] == "candidate":
        out.update(
            {
                "turnover_ratio_vs_hourly": run["turnover_vs_hourly_reference"]["ratio"],
                "net_total_return_2x_costs": run["portfolio_at_stressed_costs"]["net_total_return"],
                "dsr": run["selection_bias"]["deflated_sharpe_ratio"],
                "trials": run["selection_bias"]["trials_including_this"],
                "gates": run["gates_provisional"],
                "all_gates_passed": run["all_gates_passed"],
            }
        )
    return out

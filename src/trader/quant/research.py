"""Experiment registry and the standard strategy evaluation.

Every run — good or bad — is written to ``research/experiments/`` so the number of trials is known when
selection-bias statistics are computed. The holdout period is excluded unless explicitly unlocked.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trader.config import CostConfig
from trader.domain import new_id
from trader.quant.backtest import BacktestResult, run_backtest
from trader.quant.stats import (
    block_bootstrap_ci,
    deflated_sharpe_ratio,
    paired_sharpe_difference_ci,
    performance_metrics,
    probabilistic_sharpe_ratio,
    sharpe_per_period,
)
from trader.quant.strategies import (
    STRATEGIES,
    StrategySignal,
    daily_rebalance_volatility_signal,
    volatility_scaled_signal,
)
from trader.timeutil import bars_per_year, from_ms, iso, to_ms, utc_now

BENCHMARK_STRATEGIES = frozenset({"buy_and_hold", "no_trade"})

STANDARD_LIMITATIONS = [
    "Universe chosen with hindsight (large caps that survived); results are conditional on that selection.",
    "Fees, spread and slippage are provisional assumptions, not measured fills.",
    "No partial fills, latency, queue position or market impact; fills at next bar open.",
    "Intrabar order of high/low is unknown; stops are assumed hit when the low touches them.",
    "Single-symbol long/flat simulation; no portfolio effects.",
    "A backtest is not evidence of future profitability. Passing gates justifies paper trading only.",
]


def min_evaluation_trades(validation: dict[str, float]) -> int:
    """Trade-count gate. ``min_oos_trades`` is the pre-2026-11-11 name, still read for old configs."""
    return int(validation.get("min_evaluation_trades", validation.get("min_oos_trades", 100)))


def holdout_start_ms(holdout_start: str) -> int:
    return to_ms(datetime.fromisoformat(holdout_start).replace(tzinfo=UTC))


def code_commit(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def working_tree_dirty(root: Path) -> bool | None:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class ExperimentRegistry:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def record(self, kind: str, payload: dict[str, Any]) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        experiment_id = new_id("exp")
        record = {"experiment_id": experiment_id, "kind": kind, "created_at": iso(utc_now()), **payload}
        path = self.directory / f"{experiment_id}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return experiment_id

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        records = []
        for path in sorted(self.directory.glob("exp_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if kind is None or record.get("kind") == kind:
                records.append(record)
        return sorted(records, key=lambda r: r.get("created_at", ""))

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        if not experiment_id.replace("_", "").isalnum():
            return None
        path = self.directory / f"{experiment_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def trials(self) -> list[dict[str, Any]]:
        """Every registered evaluation that counts toward the DSR trial count.

        Counted: single-strategy backtests (except the benchmarks ``buy_and_hold`` / ``no_trade``, which are
        references, not candidates), volatility-overlay results, each configuration of a PBO family, each ML model
        evaluation (no Sharpe: it adds to N but not to the Sharpe variance) and portfolio candidates.
        Not counted: records marked ``diagnostic_only`` / ``counts_toward_dsr: false``, portfolio references, and
        invocations that failed before producing any result (nothing was looked at).
        """
        out: list[dict[str, Any]] = []

        def add(record: dict[str, Any], sharpe: Any) -> None:
            finite = isinstance(sharpe, int | float) and bool(np.isfinite(sharpe))
            out.append(
                {
                    "experiment_id": record.get("experiment_id"),
                    "kind": record.get("kind"),
                    "interval": record.get("interval") or "1h",
                    "sharpe_per_period": float(sharpe) if finite else None,
                }
            )

        for record in self.records():
            if record.get("diagnostic_only") or record.get("counts_toward_dsr") is False:
                continue
            kind = record.get("kind")
            if kind == "backtest":
                if record.get("strategy") not in BENCHMARK_STRATEGIES:
                    add(record, record.get("development", {}).get("sharpe_per_period"))
            elif kind == "volatility_overlay":
                sharpe = record.get("overlay", {}).get("sharpe_per_period")
                if sharpe is not None:
                    add(record, sharpe)
            elif kind == "pbo":  # every configuration of a parameter family is a trial too
                for sharpe in record.get("config_sharpes_per_period", []):
                    add(record, sharpe)
            elif kind == "ml_model":
                if record.get("status", "ok") == "ok":
                    add(record, None)
            elif (
                kind == "portfolio_backtest"
                and record.get("role") == "candidate"
                and record.get("status", "ok") == "ok"
            ):
                add(record, record.get("portfolio", {}).get("sharpe_per_period"))
        return out

    def trial_count(self) -> int:
        return len(self.trials())

    def trial_sharpes(self, target_interval: str | None = None) -> list[float]:
        """Finite per-period Sharpe ratios of counted trials.

        With ``target_interval``, each value is rescaled from its own bar interval to that interval
        (x sqrt(bars_per_year(source) / bars_per_year(target))) so trials on different clocks share units.
        """
        values = []
        for trial in self.trials():
            sharpe = trial["sharpe_per_period"]
            if sharpe is None:
                continue
            if target_interval is not None:
                sharpe *= math.sqrt(bars_per_year(trial["interval"]) / bars_per_year(target_interval))
            values.append(sharpe)
        return values


def _signal_slice(signal: StrategySignal, start: int, end: int) -> StrategySignal:
    return StrategySignal(
        signal.name,
        signal.version,
        signal.target.iloc[start:end].reset_index(drop=True),
        signal.atr.iloc[start:end].reset_index(drop=True),
        signal.stop_mult,
        signal.params,
        signal.detail.iloc[start:end].reset_index(drop=True),
    )


def evaluate_volatility_overlay(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    base_strategy: str,
    params: dict[str, float] | None,
    costs: CostConfig,
    holdout_start: str,
    validation: dict[str, float],
    registry: ExperimentRegistry,
    root: Path,
    train_days: int = 730,
    test_days: int = 90,
    vol_lookback_days: int = 30,
    rebalance_band: float = 0.0,
    daily_rebalance: bool = False,
    n_boot: int = 500,
    data_quality: dict[str, Any] | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Test trailing-RV exposure with rolling folds, optionally suppressing small rebalances."""
    if base_strategy not in {"tsmom_trend_v1", "donchian_breakout_v1"}:
        raise ValueError("volatility overlay supports only the two rule baselines")
    if not np.isfinite(rebalance_band) or not 0.0 <= rebalance_band < 1.0:
        raise ValueError("rebalance_band must be finite and in [0, 1)")
    if daily_rebalance and rebalance_band > 0.0:
        raise ValueError("daily_rebalance and rebalance_band are separate preregistered variants")
    cutoff = holdout_start_ms(holdout_start)
    dev = candles[candles["open_time"] < cutoff].reset_index(drop=True)
    bars_year = bars_per_year(interval)
    train_bars = int(train_days * bars_year / 365)
    test_bars = int(test_days * bars_year / 365)
    vol_bars = int(vol_lookback_days * bars_year / 365)
    if len(dev) < train_bars + test_bars or min(train_bars, test_bars, vol_bars) < 2:
        raise ValueError("insufficient development history for a complete walk-forward test fold")

    base = STRATEGIES[base_strategy](dev, params)
    close = pd.Series(dev["close"].to_numpy(dtype=float), index=dev.index)
    log_returns = pd.Series(np.log(close / close.shift(1)), index=dev.index)
    rv = log_returns.rolling(vol_bars, min_periods=vol_bars).std() * np.sqrt(bars_year)
    fold_bounds: list[tuple[int, int, float]] = []
    target_vol = pd.Series(np.nan, index=dev.index, dtype=float)
    test_start = train_bars
    while test_start < len(dev):
        test_end = min(test_start + test_bars, len(dev))
        train_start = max(0, test_start - train_bars)
        calibration = rv.iloc[train_start:test_start].dropna()
        if len(calibration) < max(30, vol_bars):
            raise ValueError("training fold has too little valid trailing volatility history")
        target = float(calibration.median())
        # The prior close supplies the signal for the first test-bar open.
        target_vol.iloc[test_start - 1 : test_end - 1] = target
        fold_bounds.append((test_start, test_end, target))
        test_start = test_end
    if len(fold_bounds) < 4:
        raise ValueError("walk-forward evaluation requires at least four OOS folds")

    hourly_challenger = volatility_scaled_signal(
        dev,
        base,
        interval,
        target_vol,
        lookback_bars=vol_bars,
    )
    challenger = (
        daily_rebalance_volatility_signal(dev, hourly_challenger, interval)
        if daily_rebalance
        else hourly_challenger
    )
    oos_start, oos_end = fold_bounds[0][0], fold_bounds[-1][1]
    slice_start, slice_end = oos_start - 1, oos_end
    eval_candles = dev.iloc[slice_start:slice_end].reset_index(drop=True)
    base_eval = _signal_slice(base, slice_start, slice_end)
    challenger_eval = _signal_slice(challenger, slice_start, slice_end)
    cost = costs.per_side_fraction()
    base_result = run_backtest(eval_candles, base_eval, cost, cost_components=cost_component_rates(costs))
    hourly_eval = _signal_slice(hourly_challenger, slice_start, slice_end)
    reference_result = run_backtest(eval_candles, hourly_eval, cost, cost_components=cost_component_rates(costs))
    schedule_column = "rebalance_scheduled" if daily_rebalance else None
    challenger_result = run_backtest(
        eval_candles,
        challenger_eval,
        cost,
        rebalance_band=rebalance_band,
        rebalance_schedule_column=schedule_column,
        cost_components=cost_component_rates(costs),
    )
    stress_multiplier = float(validation.get("cost_stress_multiplier", 2.0))
    stress_cost = costs.per_side_fraction(stress_multiplier)
    stressed = run_backtest(
        eval_candles,
        challenger_eval,
        stress_cost,
        rebalance_band=rebalance_band,
        rebalance_schedule_column=schedule_column,
        cost_components=cost_component_rates(costs, stress_multiplier),
    )
    ppy = bars_per_year(interval)
    base_metrics = _summarise(base_result, interval, test_days, float(validation.get("bootstrap_ci", 0.90)))
    overlay_metrics = _summarise(
        challenger_result, interval, test_days, float(validation.get("bootstrap_ci", 0.90))
    )
    reference_metrics = _summarise(
        reference_result, interval, test_days, float(validation.get("bootstrap_ci", 0.90))
    )
    stressed_metrics = _summarise(stressed, interval, test_days, float(validation.get("bootstrap_ci", 0.90)))
    sharpe_diff_ci = paired_sharpe_difference_ci(
        base_result.returns.iloc[1:].to_numpy(dtype=float),
        challenger_result.returns.iloc[1:].to_numpy(dtype=float),
        ppy,
        block=max(1, int(ppy / 365)),
        n_boot=n_boot,
        ci=float(validation.get("bootstrap_ci", 0.90)),
    )

    fold_results = []
    for start, end, calibration_target in fold_bounds:
        local_start, local_end = start - slice_start, end - slice_start
        fold_rows: dict[str, Any] = {"start_bar": int(start), "end_bar_exclusive": int(end),
                                     "target_volatility": calibration_target}
        fold_results_for_metrics = [("baseline", base_result), ("overlay", challenger_result)]
        if rebalance_band > 0.0 or daily_rebalance:
            fold_results_for_metrics.append(("hourly_overlay_reference", reference_result))
        for label, result in fold_results_for_metrics:
            fold_metrics = performance_metrics(
                result.returns.iloc[local_start:local_end],
                result.in_position.iloc[local_start:local_end],
                [],
                ppy,
                result.turnover.iloc[local_start:local_end],
                result.turnover_by_reason.iloc[local_start:local_end],
                result.modeled_costs_by_component_and_reason.iloc[local_start:local_end],
            )
            fold_rows[label] = {
                key: fold_metrics[key]
                for key in ("net_total_return", "sharpe", "sortino", "max_drawdown", "exposure", "turnover_per_year")
            }
        fold_results.append(fold_rows)

    prior = registry.trial_sharpes()
    all_sharpes = [*prior, float(overlay_metrics["sharpe_per_period"])]
    n_trials = registry.trial_count() + 1
    finite = [s for s in all_sharpes if np.isfinite(s)]
    variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
    dsr = deflated_sharpe_ratio(
        challenger_result.returns.iloc[1:].fillna(0.0).to_numpy(dtype=float), n_trials, variance
    )
    improved_folds = sum(
        row["overlay"]["sharpe"] is not None
        and row["baseline"]["sharpe"] is not None
        and row["overlay"]["sharpe"] > row["baseline"]["sharpe"]
        for row in fold_results
    )
    gates = {
        "enough_trades": overlay_metrics["trades"] >= min_evaluation_trades(validation),
        "sharpe_ci_above_zero": bool(
            np.isfinite(overlay_metrics["sharpe_ci_annualized"][0])
            and overlay_metrics["sharpe_ci_annualized"][0] > 0
        ),
        "dsr_at_least_min": bool(np.isfinite(dsr) and dsr >= validation.get("min_dsr", 0.5)),
        "positive_at_stressed_costs": stressed_metrics["net_total_return"] > 0,
        "beats_baseline_sharpe_ci": bool(np.isfinite(sharpe_diff_ci[0]) and sharpe_diff_ci[0] > 0),
        "beats_baseline_in_two_thirds_of_folds": improved_folds >= math.ceil(2 * len(fold_results) / 3),
    }
    policy_research_checks: dict[str, Any] = {}
    if rebalance_band > 0.0 or daily_rebalance:
        turnover_reduction = (
            1.0 - overlay_metrics["turnover_per_year"] / reference_metrics["turnover_per_year"]
            if reference_metrics["turnover_per_year"] > 0
            else 0.0
        )
        return_difference = overlay_metrics["net_total_return"] - reference_metrics["net_total_return"]
        overlay_difference_ci = paired_sharpe_difference_ci(
            reference_result.returns.iloc[1:].to_numpy(dtype=float),
            challenger_result.returns.iloc[1:].to_numpy(dtype=float),
            ppy,
            block=max(1, int(ppy / 365)),
            n_boot=n_boot,
            ci=float(validation.get("bootstrap_ci", 0.90)),
        )
        policy_research_checks = {
            "turnover_reduction_fraction": turnover_reduction,
            "turnover_reduced_at_least_25pct": turnover_reduction >= 0.25,
            "net_total_return_difference_vs_unbanded": return_difference,
            "net_total_return_within_5pp_of_unbanded": return_difference >= -0.05,
            "paired_sharpe_difference_ci_vs_unbanded_annualized": list(overlay_difference_ci),
            "exploratory_only_oos_previously_viewed": True,
        }
    if daily_rebalance:
        hypothesis = f"daily-only volatility resizing reduces hourly turnover on {symbol} {interval}"
        strategy_name = f"vol_scaled_{base_strategy}_daily"
        protocol_path = "research/protocols/volatility-daily-rebalance-v1.md"
    elif rebalance_band > 0.0:
        hypothesis = f"30-day volatility scaling with a {rebalance_band:.0%} no-trade band reduces turnover on {symbol} {interval}"
        strategy_name = f"vol_scaled_{base_strategy}_band"
        protocol_path = "research/protocols/volatility-rebalance-band-v1.md"
    else:
        hypothesis = f"30-day volatility scaling improves {base_strategy} risk-adjusted returns on {symbol} {interval}"
        strategy_name = f"vol_scaled_{base_strategy}"
        protocol_path = None
    payload: dict[str, Any] = {
        "transaction_attribution_version": 1,
        "hypothesis": hypothesis,
        "symbol": symbol,
        "interval": interval,
        "strategy": strategy_name,
        "base_strategy": base_strategy,
        "params": {**base.params, "train_days": train_days, "test_days": test_days,
                   "vol_lookback_days": vol_lookback_days, "exposure_cap": 1.0,
                   "rebalance_band": rebalance_band, "daily_rebalance": daily_rebalance,
                   "rebalance_interval_hours": 24.0 if daily_rebalance else 0.0},
        "code_commit": code_commit(root),
        "working_tree_dirty": working_tree_dirty(root),
        **({"preregistered_protocol": protocol_path} if protocol_path else {}),
        "data": {
            "first_bar": iso(from_ms(int(dev["open_time"].iloc[0]))),
            "last_development_bar": iso(from_ms(int(dev["open_time"].iloc[-1]))),
            "development_bars": len(dev),
            "oos_start": iso(from_ms(int(dev["open_time"].iloc[oos_start]))),
            "oos_end": iso(from_ms(int(dev["open_time"].iloc[oos_end - 1]))),
            "holdout_start": holdout_start,
            "holdout_bars": int((candles["open_time"] >= cutoff).sum()),
        },
        "cost_model_bps_per_side": asdict_costs(costs),
        "data_quality": data_quality or {"status": "not_provided"},
        "walk_forward": {"train_days": train_days, "test_days": test_days, "folds": len(fold_results),
                         "vol_lookback_days": vol_lookback_days, "normalizer": "median trailing volatility in prior training window"},
        "baseline": base_metrics,
        "overlay": overlay_metrics,
        **(
            {"hourly_overlay_reference": reference_metrics}
            if daily_rebalance
            else {"unbanded_overlay_reference": reference_metrics}
            if rebalance_band > 0.0
            else {}
        ),
        **(
            {"preregistered_policy_checks": policy_research_checks}
            if rebalance_band > 0.0 or daily_rebalance
            else {}
        ),
        "overlay_at_stressed_costs": {"multiplier": float(validation.get("cost_stress_multiplier", 2.0)), **stressed_metrics},
        "paired_sharpe_difference_ci_annualized": list(sharpe_diff_ci),
        "folds": fold_results,
        "selection_bias": {"trials_including_this": n_trials, "sharpe_variance_across_trials": variance,
                           "deflated_sharpe_ratio": dsr},
        "trade_count_gate": {"min_evaluation_trades": min_evaluation_trades(validation),
                             "trades": overlay_metrics["trades"],
                             "period": "walk-forward OOS test folds of the development period"},
        "gates_provisional": gates,
        "all_gates_passed": all(gates.values()),
        "holdout": {"status": "locked"},
        "limitations": [
            *STANDARD_LIMITATIONS,
            "Volatility scaling is an exposure-management hypothesis; a risk-adjusted improvement is not proof of alpha."
        ],
    }
    if record:
        payload["experiment_id"] = registry.record("volatility_overlay", payload)
    else:
        payload["_oos_returns"] = challenger_result.returns.iloc[1:].fillna(0.0).to_numpy(dtype=float)
    return payload


def record_volatility_overlay_family(
    results: list[dict[str, Any]], registry: ExperimentRegistry, min_dsr: float = 0.5
) -> list[dict[str, Any]]:
    """Register a predeclared batch with DSR using the complete family trial count and variance."""
    historical = registry.trial_sharpes()
    family = [float(result["overlay"]["sharpe_per_period"]) for result in results]
    all_sharpes = [*historical, *family]
    n_trials = registry.trial_count() + len(family)
    finite = [value for value in all_sharpes if np.isfinite(value)]
    variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
    for result in results:
        returns = result.pop("_oos_returns")
        dsr = deflated_sharpe_ratio(returns, n_trials, variance)
        result["selection_bias"] = {
            "trials_including_this": n_trials,
            "sharpe_variance_across_trials": variance,
            "deflated_sharpe_ratio": dsr,
            "note": "DSR counts all earlier registered trials and every candidate in this predeclared batch.",
        }
        result["gates_provisional"]["dsr_at_least_min"] = bool(np.isfinite(dsr) and dsr >= min_dsr)
        result["all_gates_passed"] = all(result["gates_provisional"].values())
        result["experiment_id"] = registry.record("volatility_overlay", result)
    return results


def _slice(
    candles: pd.DataFrame, signal: StrategySignal, mask: pd.Series
) -> tuple[pd.DataFrame, StrategySignal]:
    part = candles[mask].reset_index(drop=True)
    sliced = StrategySignal(
        signal.name,
        signal.version,
        signal.target[mask].reset_index(drop=True),
        signal.atr[mask].reset_index(drop=True),
        signal.stop_mult,
        signal.params,
        signal.detail[mask].reset_index(drop=True),
    )
    return part, sliced


def _summarise(result: BacktestResult, interval: str, block_days: int, ci: float) -> dict[str, Any]:
    ppy = bars_per_year(interval)
    metrics = performance_metrics(
        result.returns,
        result.in_position,
        result.trades,
        ppy,
        result.turnover,
        result.turnover_by_reason,
        result.modeled_costs_by_component_and_reason,
    )
    r = result.returns.fillna(0.0).to_numpy()
    block = max(1, int(block_days * ppy / 365))
    block_returns = [
        float(np.prod(1 + r[i : i + block]) - 1)
        for i in range(0, len(r), block)
        if len(r[i : i + block]) >= block // 2
    ]
    sharpe_lo, sharpe_hi = block_bootstrap_ci(r, "sharpe", ci=ci)
    scale = float(np.sqrt(ppy))
    metrics.update(
        {
            "sharpe_per_period": sharpe_per_period(r),
            "psr_vs_zero": probabilistic_sharpe_ratio(r, 0.0),
            "sharpe_ci_annualized": [sharpe_lo * scale, sharpe_hi * scale],
            "sharpe_ci_level": ci,
            "sub_periods": {
                "block_days": block_days,
                "count": len(block_returns),
                "positive_share": float(np.mean([b > 0 for b in block_returns])) if block_returns else None,
                "worst": min(block_returns) if block_returns else None,
                "median": float(np.median(block_returns)) if block_returns else None,
            },
        }
    )
    return metrics


def evaluate_strategy(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    strategy: str,
    params: dict[str, float] | None,
    costs: CostConfig,
    holdout_start: str,
    validation: dict[str, float],
    registry: ExperimentRegistry,
    root: Path,
    unlock_holdout: bool = False,
    block_days: int = 90,
    hypothesis: str = "",
) -> dict[str, Any]:
    """Run one strategy on the development period (and the holdout only if unlocked); log the trial."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; available: {sorted(STRATEGIES)}")
    cutoff = holdout_start_ms(holdout_start)
    signal = STRATEGIES[strategy](candles, params)
    dev_mask = candles["open_time"] < cutoff
    ci = float(validation.get("bootstrap_ci", 0.90))
    stress = float(validation.get("cost_stress_multiplier", 2.0))

    dev_candles, dev_signal = _slice(candles, signal, dev_mask)
    if len(dev_candles) < 1000:
        raise ValueError(f"only {len(dev_candles)} development bars before the holdout; need >= 1000")
    dev_result = run_backtest(
        dev_candles, dev_signal, costs.per_side_fraction(), cost_components=cost_component_rates(costs)
    )
    development = _summarise(dev_result, interval, block_days, ci)
    stressed = run_backtest(
        dev_candles,
        dev_signal,
        costs.per_side_fraction(stress),
        cost_components=cost_component_rates(costs, stress),
    )
    stress_metrics = performance_metrics(
        stressed.returns,
        stressed.in_position,
        stressed.trades,
        bars_per_year(interval),
        stressed.turnover,
        stressed.turnover_by_reason,
        stressed.modeled_costs_by_component_and_reason,
    )

    is_benchmark = strategy in BENCHMARK_STRATEGIES
    prior = registry.trial_sharpes()
    all_sharpes = prior if is_benchmark else [*prior, development["sharpe_per_period"]]
    finite = [s for s in all_sharpes if np.isfinite(s)]
    n_trials = registry.trial_count() + (0 if is_benchmark else 1)
    variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
    dsr = deflated_sharpe_ratio(dev_result.returns.fillna(0.0).to_numpy(), n_trials, variance)

    gates = {
        "enough_trades": development["trades"] >= min_evaluation_trades(validation),
        "sharpe_ci_above_zero": bool(
            np.isfinite(development["sharpe_ci_annualized"][0]) and development["sharpe_ci_annualized"][0] > 0
        ),
        "dsr_at_least_min": bool(np.isfinite(dsr) and dsr >= validation.get("min_dsr", 0.5)),
        "positive_at_stressed_costs": stress_metrics["net_total_return"] > 0,
    }
    payload: dict[str, Any] = {
        "transaction_attribution_version": 1,
        "hypothesis": hypothesis or f"{strategy} earns a positive net return on {symbol} {interval}",
        "symbol": symbol,
        "interval": interval,
        "strategy": strategy,
        "params": signal.params,
        "code_commit": code_commit(root),
        "data": {
            "first_bar": iso(from_ms(int(candles["open_time"].iloc[0]))),
            "last_bar": iso(from_ms(int(candles["open_time"].iloc[-1]))),
            "development_bars": int(dev_mask.sum()),
            "holdout_start": holdout_start,
            "holdout_bars": int((~dev_mask).sum()),
        },
        "cost_model_bps_per_side": asdict_costs(costs),
        "development": development,
        "development_at_stressed_costs": {
            "multiplier": stress,
            "net_total_return": stress_metrics["net_total_return"],
            "sharpe": stress_metrics["sharpe"],
            "max_drawdown": stress_metrics["max_drawdown"],
        },
        "selection_bias": {
            "trials_including_this": n_trials,
            "sharpe_variance_across_trials": variance,
            "deflated_sharpe_ratio": dsr,
            "note": "DSR uses the raw trial count as if trials were independent (conservative). PBO needs a configuration family; see `trader pbo`.",
            "counts_as_trial": not is_benchmark,
        },
        "trade_count_gate": {
            "min_evaluation_trades": min_evaluation_trades(validation),
            "trades": development["trades"],
            "period": "development period (all pre-holdout bars; single pass, no walk-forward split)",
        },
        "gates_provisional": gates,
        "all_gates_passed": all(gates.values()),
        "holdout": {"status": "locked"},
        "limitations": STANDARD_LIMITATIONS,
    }
    if unlock_holdout:
        hold_candles, hold_signal = _slice(candles, signal, ~dev_mask)
        if len(hold_candles) >= 200:
            hold = run_backtest(
                hold_candles, hold_signal, costs.per_side_fraction(), cost_components=cost_component_rates(costs)
            )
            payload["holdout"] = {
                "status": "UNLOCKED - this period is no longer untouched",
                **_summarise(hold, interval, block_days, ci),
            }
        else:
            payload["holdout"] = {"status": "unlocked_but_insufficient_data", "bars": len(hold_candles)}
    payload["experiment_id"] = registry.record("backtest", payload)
    return payload


def asdict_costs(costs: CostConfig) -> dict[str, float]:
    return {"fee": costs.fee_bps, "half_spread": costs.half_spread_bps, "slippage": costs.slippage_bps}


def cost_component_rates(costs: CostConfig, multiplier: float = 1.0) -> dict[str, float]:
    return {
        "fee": costs.fee_bps * multiplier / 10_000,
        "half_spread": costs.half_spread_bps * multiplier / 10_000,
        "slippage": costs.slippage_bps * multiplier / 10_000,
    }


def trade_dicts(result: BacktestResult, limit: int = 50) -> list[dict[str, Any]]:
    return [asdict(t) for t in result.trades[-limit:]]

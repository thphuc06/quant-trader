"""Experiment registry and the standard strategy evaluation.

Every run — good or bad — is written to ``research/experiments/`` so the number of trials is known when
selection-bias statistics are computed. The holdout period is excluded unless explicitly unlocked.
"""

from __future__ import annotations

import json
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
    performance_metrics,
    probabilistic_sharpe_ratio,
    sharpe_per_period,
)
from trader.quant.strategies import STRATEGIES, StrategySignal
from trader.timeutil import bars_per_year, from_ms, iso, to_ms, utc_now

STANDARD_LIMITATIONS = [
    "Universe chosen with hindsight (large caps that survived); results are conditional on that selection.",
    "Fees, spread and slippage are provisional assumptions, not measured fills.",
    "No partial fills, latency, queue position or market impact; fills at next bar open.",
    "Intrabar order of high/low is unknown; stops are assumed hit when the low touches them.",
    "Single symbol, fully invested when long; no portfolio effects.",
    "A backtest is not evidence of future profitability. Passing gates justifies paper trading only.",
]


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

    def trial_sharpes(self) -> list[float]:
        values = []
        for record in self.records("backtest"):
            sr = record.get("development", {}).get("sharpe_per_period")
            if isinstance(sr, int | float) and np.isfinite(sr):
                values.append(float(sr))
        for record in self.records("pbo"):  # every configuration of a parameter family is a trial too
            values.extend(
                float(s)
                for s in record.get("config_sharpes_per_period", [])
                if isinstance(s, int | float) and np.isfinite(s)
            )
        return values


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
    metrics = performance_metrics(result.returns, result.in_position, result.trades, ppy)
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
    dev_result = run_backtest(dev_candles, dev_signal, costs.per_side_fraction())
    development = _summarise(dev_result, interval, block_days, ci)
    stressed = run_backtest(dev_candles, dev_signal, costs.per_side_fraction(stress))
    stress_metrics = performance_metrics(
        stressed.returns, stressed.in_position, stressed.trades, bars_per_year(interval)
    )

    prior = registry.trial_sharpes()
    all_sharpes = [*prior, development["sharpe_per_period"]]
    finite = [s for s in all_sharpes if np.isfinite(s)]
    n_trials = len(all_sharpes)
    variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
    dsr = deflated_sharpe_ratio(dev_result.returns.fillna(0.0).to_numpy(), n_trials, variance)

    gates = {
        "enough_trades": development["trades"] >= validation.get("min_oos_trades", 100),
        "sharpe_ci_above_zero": bool(
            np.isfinite(development["sharpe_ci_annualized"][0]) and development["sharpe_ci_annualized"][0] > 0
        ),
        "dsr_at_least_min": bool(np.isfinite(dsr) and dsr >= validation.get("min_dsr", 0.5)),
        "positive_at_stressed_costs": stress_metrics["net_total_return"] > 0,
    }
    payload: dict[str, Any] = {
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
        },
        "gates_provisional": gates,
        "all_gates_passed": all(gates.values()),
        "holdout": {"status": "locked"},
        "limitations": STANDARD_LIMITATIONS,
    }
    if unlock_holdout:
        hold_candles, hold_signal = _slice(candles, signal, ~dev_mask)
        if len(hold_candles) >= 200:
            hold = run_backtest(hold_candles, hold_signal, costs.per_side_fraction())
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


def trade_dicts(result: BacktestResult, limit: int = 50) -> list[dict[str, Any]]:
    return [asdict(t) for t in result.trades[-limit:]]

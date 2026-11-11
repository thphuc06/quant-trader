"""Runner for research/protocols/daily-baselines-backext-v1.md (H1). Everything the protocol fixes is a constant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from trader.config import CostConfig, RiskPolicy
from trader.quant import framework_v2
from trader.quant.daily_baselines import DAILY_PARAMS, compare_with_native, resample_daily
from trader.quant.portfolio import PortfolioLimits, PortfolioResult, run_portfolio_backtest
from trader.quant.research import (
    ExperimentRegistry,
    asdict_costs,
    code_commit,
    cost_component_rates,
    mark_supersedes,
    working_tree_dirty,
)
from trader.quant.stats import sharpe_per_period
from trader.quant.strategies import STRATEGIES
from trader.timeutil import from_ms, iso, to_ms

PROTOCOL_PATH = "research/protocols/daily-baselines-backext-v1.md"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DATA_START = (
    "2017-07-01T00:00:00+00:00"  # before the first Binance candle; the API returns from the first one
)
EVALUATION_START = "2017-12-01T00:00:00+00:00"
EVALUATION_END = "2021-11-11T00:00:00+00:00"  # exclusive; the main store starts on this day
PRIMARY_COST_MULTIPLIER = 2.0
STORE_DIRNAME = "candles_pre2021"


def _ms(timestamp: str) -> int:
    return to_ms(pd.Timestamp(timestamp).to_pydatetime())


def evaluate_backext(
    hourly: dict[str, pd.DataFrame],
    *,
    policy: RiskPolicy,
    costs: CostConfig,
    registry: ExperimentRegistry,
    root: Path,
    native_daily: dict[str, pd.DataFrame] | None = None,
    data_quality: dict[str, Any] | None = None,
    v2_params: dict[str, Any] | None = None,
    record: bool = True,
    supersedes: dict[str, str] | None = None,
    correction_defect: str = "",
) -> list[dict[str, Any]]:
    if sorted(hourly) != sorted(SYMBOLS):
        raise ValueError(f"protocol universe is {SYMBOLS}")
    start_ms, end_ms = _ms(EVALUATION_START), _ms(EVALUATION_END)
    for symbol, frame in hourly.items():
        if frame.empty or int(frame["open_time"].max()) >= end_ms:
            raise ValueError(f"{symbol}: hourly data must end before {EVALUATION_END}")
    daily: dict[str, pd.DataFrame] = {}
    aggregation: dict[str, Any] = {}
    for symbol, frame in hourly.items():
        daily[symbol], aggregation[symbol] = resample_daily(frame)
        aggregation[symbol]["first_hourly_bar"] = iso(from_ms(int(frame["open_time"].min())))
        if native_daily is not None:
            aggregation[symbol]["native_1d_check"] = compare_with_native(
                daily[symbol], native_daily.get(symbol, pd.DataFrame(columns=["open_time"])), end_ms
            )
    limits = PortfolioLimits.from_policy(policy)
    primary_rates = cost_component_rates(costs, PRIMARY_COST_MULTIPLIER)
    base_rates = cost_component_rates(costs, 1.0)

    def simulate(signals: dict[str, Any], rates: dict[str, float]) -> PortfolioResult:
        return run_portfolio_backtest(
            daily, signals, interval="1d", limits=limits, cost_components=rates, start_time_ms=start_ms,
            end_time_ms=end_ms, priority=SYMBOLS, clip_stop_to_policy=True,
        )  # fmt: skip

    results: dict[str, tuple[PortfolioResult, PortfolioResult]] = {}
    for strategy, params in DAILY_PARAMS.items():
        signals = {s: STRATEGIES[strategy](daily[s], params) for s in SYMBOLS}
        results[strategy] = (simulate(signals, primary_rates), simulate(signals, base_rates))

    binding_sharpes = [
        sharpe_per_period(framework_v2.daily_returns(primary).to_numpy()) for primary, _ in results.values()
    ]
    replaced = frozenset((supersedes or {}).values())
    raw_sharpes = [*registry.trial_sharpes(target_interval="1d", exclude=replaced), *binding_sharpes]
    raw_n = registry.trial_count(exclude=replaced) + len(results)
    commit, dirty = code_commit(root), working_tree_dirty(root)
    per_side_primary = sum(primary_rates.values())

    runs: list[dict[str, Any]] = []
    for strategy, (primary, base) in results.items():
        evaluation = framework_v2.evaluate(
            primary, primary, per_side_cost_primary=per_side_primary, binding_n=len(results),
            binding_sharpes=binding_sharpes, raw_n=raw_n, raw_sharpes=raw_sharpes, params=v2_params,
        )  # fmt: skip
        base_summary = framework_v2.portfolio_summary(base)
        run = {
            "protocol": PROTOCOL_PATH,
            "role": "candidate",
            "framework": framework_v2.FRAMEWORK_VERSION,
            "strategy": strategy,
            "interval": "1d",
            "symbols": SYMBOLS,
            "params": DAILY_PARAMS[strategy],
            "hypothesis": f"frozen daily {strategy} shows timing skill on untouched 2017-2021 BTC/ETH data",
            "code_commit": commit,
            "working_tree_dirty": dirty,
            "cost_model_bps_per_side": asdict_costs(costs),
            "primary_cost_multiplier": PRIMARY_COST_MULTIPLIER,
            "risk_policy": {"version": policy.version, "status": policy.status, **limits.__dict__},
            "data": {
                "source": f"Binance Spot 1h klines (separate store data/{STORE_DIRNAME}); daily = complete UTC days",
                "evaluation_start": EVALUATION_START,
                "evaluation_end_exclusive": EVALUATION_END,
                "aggregation": aggregation,
                "data_quality": data_quality or {"status": "not_provided"},
            },
            **evaluation,
            "at_base_costs": {
                k: base_summary[k]
                for k in ("net_total_return", "sharpe", "max_drawdown", "trades", "turnover_per_year")
            },
            "all_gates_passed": evaluation["tier"] == "VALIDATED",
            "holdout": {"status": "locked", "note": "the 2025-11-10 holdout is not part of this protocol"},
            "limitations": [
                "Classic, widely known parameters on a well-known bull era: untouched by this desk, not by the field.",
                "Two coins; USDT left its peg at times; exchange outages appear as missing days.",
                "Costs in 2017-2018 are unverified, hence 2x costs as the primary level.",
                "Timing test reconstructs returns from daily exposures and ignores intraday stop fills.",
            ],
        }
        runs.append(run)
    mark_supersedes(runs, supersedes, correction_defect)
    if record:
        for run in runs:
            run["experiment_id"] = registry.record("portfolio_backtest", run)
    return runs


def compact(run: dict[str, Any]) -> dict[str, Any]:
    p = run["portfolio"]
    return {
        "experiment_id": run.get("experiment_id"),
        "strategy": run["strategy"],
        "tier": run["tier"],
        "gates": run["gates"],
        "net_total_return_2x": p["net_total_return"],
        "net_total_return_1x": run["at_base_costs"]["net_total_return"],
        "max_drawdown_2x": p["max_drawdown"],
        "sharpe_2x": p["sharpe"],
        "trades": p["trades"],
        "timing_p": run["timing_skill"]["p_value"],
        "timing_excess_annualized": run["timing_skill"].get("statistic_annualized"),
        "sharpe_ci_by_block": {
            k: v["sharpe_ci_annualized"] for k, v in run["daily_bootstrap"]["by_block"].items()
        },
        "implied_sharpe_threshold": run["implied_observed_sharpe_threshold"],
        "dsr_binding": run["selection_bias"]["dsr_binding"],
        "dsr_raw": run["selection_bias"]["dsr_raw_registry"],
        "mean_exposure": p["mean_gross_exposure"],
    }

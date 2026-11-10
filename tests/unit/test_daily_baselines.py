"""Runner for research/protocols/daily-baselines-v1.md on synthetic data (never on real candles)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.conftest import REPO, synthetic_candles
from trader.config import CostConfig, load_app_config, load_risk_policy
from trader.quant.daily_baselines import (
    DAILY_PARAMS,
    SYMBOL_PRIORITY,
    compact,
    compare_with_native,
    evaluate_daily_baselines,
    resample_daily,
)
from trader.quant.research import ExperimentRegistry, holdout_start_ms

HOLDOUT = "2025-11-10"
CUTOFF = holdout_start_ms(HOLDOUT)
H = 3_600_000


def _hourly(seed: int, end_ms: int = CUTOFF + 40 * 24 * H, n: int = 24 * 365 * 3) -> dict:
    return {
        s: synthetic_candles(n, end_ms=end_ms, seed=seed + i, drift=0.0001)
        for i, s in enumerate(SYMBOL_PRIORITY)
    }


def test_resample_keeps_only_complete_utc_days_and_matches_native() -> None:
    hourly = synthetic_candles(24 * 10, end_ms=CUTOFF, seed=1)
    daily, info = resample_daily(hourly)
    assert info["complete_days"] == 10 and not info["incomplete_days_dropped"]
    first_day = hourly.iloc[:24]
    assert daily["open"].iloc[0] == first_day["open"].iloc[0]
    assert daily["high"].iloc[0] == first_day["high"].max()
    assert daily["close"].iloc[0] == first_day["close"].iloc[-1]
    assert compare_with_native(daily, daily.copy(), CUTOFF)["status"] == "ok"
    gapped, info = resample_daily(hourly.drop(index=30))  # one hour missing on day 2
    assert len(gapped) == 9 and len(info["incomplete_days_dropped"]) == 1


def test_protocol_run_is_holdout_blind_and_records_two_trials(tmp_path: Path) -> None:
    app = load_app_config(REPO)
    policy = load_risk_policy(REPO).policy
    registry = ExperimentRegistry(tmp_path / "exp")
    registry.record("backtest", {"strategy": "tsmom_trend_v1", "development": {"sharpe_per_period": 0.01}})

    def run(data: dict, record: bool) -> list[dict]:
        return evaluate_daily_baselines(
            data, policy=policy, costs=CostConfig(), hourly_params=app.strategies, holdout_start=HOLDOUT,
            validation=app.validation, registry=registry, root=tmp_path, n_boot=100, record=record,
        )  # fmt: skip

    clean = _hourly(seed=21)
    poisoned = {s: f.copy() for s, f in clean.items()}
    for frame in poisoned.values():
        frame.loc[frame["open_time"] >= CUTOFF, ["open", "high", "low", "close"]] *= 50.0
    first, second = run(clean, record=False), run(poisoned, record=True)
    for a, b in zip(first, second, strict=True):
        assert a["portfolio"]["net_total_return"] == pytest.approx(b["portfolio"]["net_total_return"])

    assert [r["role"] for r in second] == ["reference", "candidate", "reference", "candidate"]
    candidates = [r for r in second if r["role"] == "candidate"]
    assert [c["params"] for c in candidates] == list(DAILY_PARAMS.values())
    assert all(c["selection_bias"]["trials_including_this"] == 1 + 2 for c in candidates)
    assert all(set(c["gates_provisional"]) == {"turnover_cut_at_least_half", "enough_trades", "sharpe_ci_above_zero",
                                                 "dsr_at_least_min", "positive_at_stressed_costs"} for c in candidates)  # fmt: skip
    assert all(c["portfolio"]["evaluation_start"] == "2023-11-12T00:00:00Z" for c in second)
    assert all(c["holdout"] == {"status": "locked"} for c in second)
    assert all(np.isfinite(c["turnover_vs_hourly_reference"]["ratio"]) for c in candidates)
    # references are recorded but are not DSR trials; the two candidates are
    assert registry.trial_count() == 1 + 2
    assert len(registry.records("portfolio_backtest")) == 4
    assert compact(candidates[0])["gates"] == candidates[0]["gates_provisional"]


def test_wrong_universe_is_rejected(tmp_path: Path) -> None:
    app = load_app_config(REPO)
    with pytest.raises(ValueError):
        evaluate_daily_baselines(
            {"BTCUSDT": synthetic_candles(100)}, policy=load_risk_policy(REPO).policy, costs=CostConfig(),
            hourly_params=app.strategies, holdout_start=HOLDOUT, validation=app.validation,
            registry=ExperimentRegistry(tmp_path), root=tmp_path,
        )  # fmt: skip

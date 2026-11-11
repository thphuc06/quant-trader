"""Framework v2: timing-skill test, gates/tiers, and the H1 back-extension runner (synthetic data only)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.conftest import REPO, synthetic_candles
from trader.config import CostConfig, load_app_config, load_risk_policy
from trader.quant import backext, framework_v2
from trader.quant.daily_baselines import DAILY_PARAMS, resample_daily
from trader.quant.portfolio import PortfolioLimits, run_portfolio_backtest
from trader.quant.research import ExperimentRegistry, cost_component_rates
from trader.quant.stats import timing_skill_test
from trader.quant.strategies import tsmom_trend_v1

LIMITS = PortfolioLimits.from_policy(load_risk_policy(REPO).policy)
DAY = 86_400_000


def test_timing_test_separates_skill_from_noise_and_from_constant_exposure() -> None:
    rng = np.random.default_rng(3)
    a = rng.normal(0.0005, 0.03, size=(1000, 2))
    clairvoyant = np.where(a > 0, 0.4, 0.0)
    noise = rng.uniform(0, 0.4, size=a.shape)
    constant = np.full(a.shape, 0.2)
    zero_cost = np.zeros(1000)
    assert timing_skill_test(clairvoyant, a, zero_cost, 0.0013)["p_value"] < 0.01
    assert timing_skill_test(noise, a, zero_cost, 0.0013)["p_value"] > 0.10
    flat = timing_skill_test(constant, a, zero_cost, 0.0013)
    assert flat["p_value"] == 1.0  # shifting a constant series changes nothing: no evidence of timing
    assert flat["statistic_annualized"] == pytest.approx(
        2 * 0.0013 * 0.4 / 1000 * 365
    )  # benchmark pays a round trip
    assert timing_skill_test(constant[:50], a[:50], zero_cost[:50], 0.0013)["status"] == "insufficient_data"


def _daily_run(seed: int, rates: dict[str, float]):  # type: ignore[no-untyped-def]
    hourly = {s: synthetic_candles(24 * 900, seed=seed + i, end_ms=1_700_000_000_000, drift=0.0002)
              for i, s in enumerate(("BTCUSDT", "ETHUSDT"))}  # fmt: skip
    daily = {s: resample_daily(f)[0] for s, f in hourly.items()}
    signals = {s: tsmom_trend_v1(d, DAILY_PARAMS["tsmom_trend_v1"]) for s, d in daily.items()}
    start = int(daily["BTCUSDT"]["open_time"].iloc[200])
    return run_portfolio_backtest(
        daily, signals, interval="1d", limits=LIMITS, cost_components=rates, start_time_ms=start,
        priority=["BTCUSDT", "ETHUSDT"], clip_stop_to_policy=True,
    )  # fmt: skip


def test_portfolio_exposes_consistent_daily_series() -> None:
    result = _daily_run(5, cost_component_rates(CostConfig()))
    assert np.allclose(result.symbol_exposure.sum(axis=1), result.gross_exposure)
    assert result.bar_cost.sum() > 0 and len(result.asset_returns) == len(result.equity)
    # equity moves are explained by held exposure x asset return minus cost, except on stop/entry bars
    held = result.symbol_exposure.shift(1).fillna(0.0)
    reconstructed = (held * result.asset_returns).sum(axis=1) - result.bar_cost
    quiet = (result.turnover == 0).to_numpy()
    assert np.allclose(reconstructed[quiet], result.returns[quiet], atol=1e-9)


def test_evaluate_reports_every_gate_and_a_consistent_tier() -> None:
    rates = cost_component_rates(CostConfig(), 2.0)
    run = _daily_run(9, rates)
    out = framework_v2.evaluate(
        run, run, per_side_cost_primary=sum(rates.values()), binding_n=2, binding_sharpes=[0.02, 0.03],
        raw_n=67, raw_sharpes=[0.01] * 60 + [0.05] * 7, params={"timing_shifts": 300, "n_boot": 200},
    )  # fmt: skip
    assert set(out["gates"]) == {"G-timing", "G-cost", "G-ci", "G-dsr", "G-span"}
    gates = out["gates"]
    if not (gates["G-timing"] and gates["G-cost"]):
        assert out["tier"] == "REJECTED"
    elif all(gates.values()):
        assert out["tier"] == "VALIDATED"
    else:
        assert out["tier"] in {"INCONCLUSIVE", "REJECTED"}
    assert len(out["daily_series"]["net_return"]) == len(out["daily_series"]["close_dates"])
    assert out["selection_bias"]["binding_n"] == 2 and out["selection_bias"]["raw_n"] == 67
    assert 0.7 < framework_v2.implied_sharpe_threshold(int(3.95 * 365)) < 1.1
    with pytest.raises(ValueError):
        hourly_result = run_portfolio_backtest(
            {"A": synthetic_candles(500, seed=1)}, {"A": tsmom_trend_v1(synthetic_candles(500, seed=1))},
            interval="1h", limits=LIMITS, cost_components=rates,
            start_time_ms=int(synthetic_candles(500, seed=1)["open_time"].iloc[300]),
        )  # fmt: skip
        framework_v2.evaluate(hourly_result, hourly_result, per_side_cost_primary=0.0026, binding_n=1,
                              binding_sharpes=[], raw_n=1, raw_sharpes=[])  # fmt: skip


def test_backext_runner_records_two_framework_v2_trials_and_refuses_later_data(tmp_path: Path) -> None:
    end = backext._ms(backext.EVALUATION_END)
    n = (end - backext._ms("2017-08-17T00:00:00+00:00")) // 3_600_000
    hourly = {
        s: synthetic_candles(int(n), seed=40 + i, end_ms=end, drift=0.0001)
        for i, s in enumerate(backext.SYMBOLS)
    }
    registry = ExperimentRegistry(tmp_path / "exp")
    registry.record("backtest", {"strategy": "tsmom_trend_v1", "development": {"sharpe_per_period": 0.01}})
    app = load_app_config(REPO)
    runs = backext.evaluate_backext(
        hourly, policy=load_risk_policy(REPO).policy, costs=CostConfig(), registry=registry, root=tmp_path,
        v2_params={**app.validation_v2, "timing_shifts": 200, "n_boot": 200},
    )  # fmt: skip
    assert [r["strategy"] for r in runs] == list(DAILY_PARAMS)
    assert all(r["framework"] == "v2" and r["primary_cost_multiplier"] == 2.0 for r in runs)
    assert all(r["selection_bias"]["binding_n"] == 2 and r["selection_bias"]["raw_n"] == 3 for r in runs)
    assert all(r["portfolio"]["evaluation_start"] == "2017-12-01T00:00:00Z" for r in runs)
    assert registry.trial_count() == 3
    assert backext.compact(runs[0])["tier"] in {"REJECTED", "INCONCLUSIVE", "VALIDATED"}

    later = {s: f.copy() for s, f in hourly.items()}
    later["BTCUSDT"].loc[len(later["BTCUSDT"]) - 1, "open_time"] = end + DAY
    with pytest.raises(ValueError, match="must end before"):
        backext.evaluate_backext(
            later, policy=load_risk_policy(REPO).policy, costs=CostConfig(), registry=registry, root=tmp_path,
            record=False,
        )  # fmt: skip

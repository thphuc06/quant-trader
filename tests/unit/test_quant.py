"""Features (no look-ahead), simulator (hand-computed cases), statistics and the ML pipeline."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.conftest import synthetic_candles
from trader.config import CostConfig
from trader.quant import ml
from trader.quant.backtest import run_backtest
from trader.quant.features import FEATURE_NAMES, FEATURE_REGISTRY, atr, classify_regime, compute_features, rsi
from trader.quant.research import (
    ExperimentRegistry,
    evaluate_strategy,
    evaluate_volatility_overlay,
    record_volatility_overlay_family,
)
from trader.quant.stats import (
    block_bootstrap_ci,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    paired_sharpe_difference_ci,
    pbo_cscv,
    performance_metrics,
    probabilistic_sharpe_ratio,
)
from trader.quant.strategies import (
    STRATEGIES,
    StrategySignal,
    daily_rebalance_volatility_signal,
    donchian_breakout_v1,
    tsmom_trend_v1,
    volatility_scaled_signal,
)

SMALL = {
    "ema_fast": 5,
    "ema_slow": 20,
    "tsmom_lookback": 20,
    "rv_short": 10,
    "rv_long": 30,
    "donchian_lookback": 20,
    "volume_lookback": 20,
    "vol_regime_window": 100,
}


# ----------------------------------------------------------------------------- features


def test_features_have_no_lookahead() -> None:
    """A feature at bar t must be identical whether or not later bars exist."""
    candles = synthetic_candles(600)
    full = compute_features(candles, "1h", SMALL)
    truncated = compute_features(candles.iloc[:450].copy(), "1h", SMALL)
    pd.testing.assert_frame_equal(
        full.iloc[:450][FEATURE_NAMES], truncated[FEATURE_NAMES], check_exact=False, rtol=1e-9
    )


def test_strategy_signals_have_no_lookahead() -> None:
    candles = synthetic_candles(900)
    for build in (tsmom_trend_v1, donchian_breakout_v1):
        full = build(candles, None).target
        cut = build(candles.iloc[:700].copy(), None).target
        assert (full.iloc[:700].to_numpy() == cut.to_numpy()).all(), build.__name__


def test_recursive_indicators_converge_regardless_of_history_length() -> None:
    candles = synthetic_candles(1500)
    long_history = compute_features(candles, "1h", SMALL)
    short_history = compute_features(candles.iloc[700:].reset_index(drop=True), "1h", SMALL)
    for name in ("ema_spread", "atr", "rsi", "adx"):
        assert abs(long_history[name].iloc[-1] - short_history[name].iloc[-1]) < 1e-6 * max(
            1.0, abs(long_history[name].iloc[-1])
        ), name


def test_warmup_rows_are_nan_not_fabricated() -> None:
    features = compute_features(synthetic_candles(300), "1h", SMALL)
    assert features["ema_spread"].iloc[:19].isna().all() and features["ema_spread"].iloc[25:].notna().all()
    assert features["rv_pct"].iloc[:29].isna().all()


def test_known_indicator_values() -> None:
    rising = pd.Series(np.arange(1.0, 60.0))
    assert rsi(rising, 14).iloc[-1] == pytest.approx(100.0)
    falling = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert rsi(falling, 14).iloc[-1] == pytest.approx(0.0)
    high, low, close = pd.Series([11.0] * 40), pd.Series([9.0] * 40), pd.Series([10.0] * 40)
    assert atr(high, low, close, 14).iloc[-1] == pytest.approx(2.0)


def test_feature_registry_matches_pipeline_and_values_are_bounded() -> None:
    features = compute_features(synthetic_candles(800), "1h", SMALL).dropna()
    assert set(FEATURE_NAMES) <= set(features.columns)
    assert all(spec.formula and spec.units for spec in FEATURE_REGISTRY)
    for name in ("rsi", "adx"):
        assert features[name].between(0, 100).all()
    for name in ("donchian_pos", "taker_buy_ratio", "rv_pct"):
        assert features[name].between(0, 1).all()


def test_regime_rules_and_unknown_when_missing() -> None:
    assert (
        classify_regime(pd.Series({"ema_spread": 0.02, "adx": 30, "rv_pct": 0.9}))["label"]
        == "trend_up/vol_high"
    )
    assert (
        classify_regime(pd.Series({"ema_spread": -0.02, "adx": 30, "rv_pct": 0.1}))["label"]
        == "trend_down/vol_low"
    )
    assert (
        classify_regime(pd.Series({"ema_spread": 0.02, "adx": 10, "rv_pct": 0.5}))["label"]
        == "range/vol_normal"
    )
    assert (
        classify_regime(pd.Series({"ema_spread": np.nan, "adx": 10, "rv_pct": np.nan}))["label"]
        == "unknown/unknown"
    )


# ----------------------------------------------------------------------------- simulator


def _candles(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame["open_time"] = np.arange(len(rows), dtype="int64") * 3_600_000
    return frame


def _signal(target: list[float], atr_value: float = 1.0, stop_mult: float = 0.0) -> StrategySignal:
    n = len(target)
    return StrategySignal(
        "test",
        "1",
        pd.Series(target),
        pd.Series([atr_value] * n),
        stop_mult,
        {},
        pd.DataFrame(index=range(n)),
    )


def test_fills_happen_at_next_open_with_costs_on_both_sides() -> None:
    candles = _candles(
        [(100, 101, 99, 100), (100, 111, 100, 110), (110, 122, 110, 121), (121, 122, 120, 121)]
    )
    result = run_backtest(candles, _signal([1, 1, 0, 0]), cost_per_side=0.01)
    # decided at close of bar 0 -> buy at open of bar 1 (100); decided flat at close of bar 2 -> sell at open of bar 3 (121)
    expected = (121 / 100) * (0.99 / 1.01)
    assert result.equity.iloc[-1] == pytest.approx(expected)
    assert result.equity.iloc[0] == 1.0  # nothing can happen on the signal bar itself
    trade = result.trades[0]
    assert (trade.entry_price, trade.exit_price, trade.exit_reason, trade.bars_held) == (
        100,
        121,
        "signal",
        2,
    )
    assert trade.net_return == pytest.approx(expected - 1)


def test_turnover_and_modeled_costs_reconcile_by_event_and_component() -> None:
    candles = _candles([(100, 101, 99, 100)] * 4)
    signal = _signal([1.0, 1.0, 0.0, 0.0])
    components = {"fee": 0.005, "half_spread": 0.003, "slippage": 0.002}
    result = run_backtest(candles, signal, 0.01, cost_components=components)
    by_reason = result.turnover_by_reason
    assert by_reason["baseline_entry"].sum() > 0
    assert by_reason["baseline_exit"].sum() > 0
    assert by_reason["overlay_resize"].sum() == 0
    np.testing.assert_allclose(by_reason.sum(axis=1), result.turnover, rtol=1e-12, atol=1e-12)
    modeled_cost = result.modeled_costs_by_component_and_reason
    assert modeled_cost["fee__baseline_entry"].sum() > 0
    assert modeled_cost["half_spread__baseline_exit"].sum() > 0
    assert modeled_cost.to_numpy().sum() == pytest.approx(result.modeled_cost_initial_equity)
    metrics = performance_metrics(
        result.returns,
        result.in_position,
        result.trades,
        8760,
        result.turnover,
        result.turnover_by_reason,
        result.modeled_costs_by_component_and_reason,
    )
    assert metrics["turnover_per_year_by_reason"]["baseline_entry"] > 0
    costs = metrics["modeled_cost_bps_initial_equity_by_reason"]
    assert costs["baseline_entry"]["total"] == pytest.approx(
        costs["baseline_entry"]["fee"]
        + costs["baseline_entry"]["half_spread"]
        + costs["baseline_entry"]["slippage"]
    )
    assert costs["total"]["total"] == pytest.approx(sum(costs[r]["total"] for r in by_reason.columns))
    assert costs["total"]["total"] == pytest.approx(result.modeled_cost_initial_equity * 10_000)

    legacy_call = run_backtest(candles, signal, 0.01)
    assert "all_in__baseline_entry" in legacy_call.modeled_costs_by_component_and_reason
    with pytest.raises(ValueError, match="sum to cost_per_side"):
        run_backtest(candles, signal, 0.01, cost_components={"fee": 0.005, "slippage": 0.002})


def test_turnover_attributes_stop_touch_gap_and_resize_plus_stop() -> None:
    touched = _candles(
        [(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 97, 99), (99, 99, 99, 99)]
    )
    touched_result = run_backtest(touched, _signal([1, 1, 1, 1], stop_mult=2.0), 0.0)
    assert touched_result.turnover_by_reason["stop_touch"].sum() > 0
    assert touched_result.turnover_by_reason["stop_gap"].sum() == 0

    gapped = _candles([(100, 100, 100, 100), (100, 101, 99, 100), (95, 96, 94, 95), (95, 96, 94, 95)])
    gap_result = run_backtest(gapped, _signal([1, 1, 1, 1], stop_mult=2.0), 0.0)
    assert gap_result.turnover_by_reason["stop_gap"].sum() > 0
    assert gap_result.turnover_by_reason["stop_touch"].sum() == 0

    resize_and_stop = _candles(
        [(100, 100, 100, 100), (100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 97, 99)]
    )
    combined = run_backtest(
        resize_and_stop,
        _signal([0.5, 0.5, 0.25, 0.25], stop_mult=2.0),
        0.0,
    )
    assert combined.turnover_by_reason["overlay_resize"].sum() > 0
    assert combined.turnover_by_reason["stop_touch"].sum() > 0
    np.testing.assert_allclose(combined.turnover_by_reason.sum(axis=1), combined.turnover)


def test_flat_signal_exit_takes_precedence_over_intrabar_stop_at_same_open() -> None:
    candles = _candles([(100, 100, 100, 100), (100, 101, 99, 100), (100, 101, 97, 99)])
    result = run_backtest(candles, _signal([1.0, 0.0, 0.0], stop_mult=2.0), 0.0)
    assert result.turnover_by_reason["baseline_exit"].sum() > 0
    assert result.turnover_by_reason["stop_touch"].sum() == 0


def test_fractional_exposure_rebalances_and_charges_costs_on_traded_notional() -> None:
    candles = _candles([(100, 100, 100, 100)] * 5)
    result = run_backtest(candles, _signal([0.5, 0.5, 0.25, 0.0, 0.0]), cost_per_side=0.01)
    assert result.in_position.iloc[1] == pytest.approx(0.5)
    assert result.in_position.iloc[2] == pytest.approx(0.5)
    assert result.in_position.iloc[3] == pytest.approx(0.25)
    assert result.in_position.max() <= 1.0
    assert result.turnover.iloc[1] == pytest.approx(0.5 / 1.005)
    assert result.turnover.iloc[2] == pytest.approx(0.0)
    assert result.turnover.iloc[3] == pytest.approx(0.25 / 0.9975)
    assert result.turnover.iloc[4] == pytest.approx(0.25)
    assert result.equity.iloc[-1] < 1.0
    assert result.trades[0].exit_reason == "signal"


def test_no_trade_band_suppresses_small_rebalances_but_flat_signal_exits() -> None:
    candles = _candles([(100, 100, 100, 100)] * 5)
    result = run_backtest(candles, _signal([0.5, 0.47, 0.44, 0.0, 0.0]), 0.0, rebalance_band=0.05)
    assert result.in_position.iloc[1] == pytest.approx(0.5)
    assert result.turnover.iloc[2] == pytest.approx(0.0)  # 3pp change is inside the 5pp band
    assert result.in_position.iloc[2] == pytest.approx(0.5)
    assert result.turnover.iloc[3] == pytest.approx(0.06)  # 6pp change crosses the band
    assert result.in_position.iloc[3] == pytest.approx(0.44)
    assert result.in_position.iloc[4] == pytest.approx(0.0)  # flat signal exits immediately
    assert result.turnover.iloc[4] == pytest.approx(0.44)

    with pytest.raises(ValueError):
        run_backtest(candles, _signal([0.5] * 5), 0.0, rebalance_band=1.0)


def test_daily_rebalance_holds_sizing_until_schedule_and_exits_when_baseline_is_flat() -> None:
    candles = _candles([(100, 100, 100, 100)] * 6)
    signal = StrategySignal(
        "daily",
        "1",
        pd.Series([0.5, 0.8, 0.2, 0.3, 0.0, 0.0]),
        pd.Series([np.nan] * 6),
        0.0,
        {},
        pd.DataFrame(
            {
                "base_target": [1.0, 1.0, 1.0, 1.0, 0.0, 0.0],
                "rebalance_scheduled": [True, False, False, True, False, False],
            }
        ),
    )
    result = run_backtest(candles, signal, 0.0, rebalance_schedule_column="rebalance_scheduled")
    assert result.in_position.iloc[1] == pytest.approx(0.5)
    assert result.in_position.iloc[2] == pytest.approx(0.5)
    assert result.in_position.iloc[3] == pytest.approx(0.5)
    assert result.in_position.iloc[4] == pytest.approx(0.3)
    assert result.in_position.iloc[5] == pytest.approx(0.0)


def test_daily_volatility_multiplier_updates_only_at_utc_day_boundary() -> None:
    candles = _candles([(100, 100, 100, 100)] * 50)
    base_target = pd.Series(np.ones(50))
    scaled = StrategySignal(
        "scaled",
        "1",
        base_target,
        pd.Series([np.nan] * 50),
        0.0,
        {},
        pd.DataFrame(
            {
                "base_target": base_target,
                "realized_volatility": np.r_[np.ones(23), np.full(27, 2.0)],
                "target_volatility": np.ones(50),
            }
        ),
    )
    daily = daily_rebalance_volatility_signal(candles, scaled, "1h")
    assert daily.detail["rebalance_scheduled"].iloc[0]
    assert daily.detail["rebalance_scheduled"].iloc[23]
    assert daily.target.iloc[:23].eq(1.0).all()
    assert daily.target.iloc[23:47].eq(0.5).all()
    assert daily.target.iloc[47:].eq(0.5).all()
    future_changed = scaled.detail.copy()
    future_changed.loc[30:, "realized_volatility"] = 0.25
    changed = daily_rebalance_volatility_signal(
        candles,
        StrategySignal("scaled", "1", base_target, scaled.atr, 0.0, {}, future_changed),
        "1h",
    )
    pd.testing.assert_series_equal(daily.target.iloc[:30], changed.target.iloc[:30])


def test_stop_fills_at_stop_or_at_gap_open_and_blocks_reentry() -> None:
    # entry at 100 on bar 1, stop = 100 - 2*1 = 98
    touched = _candles(
        [
            (100, 100, 100, 100),
            (100, 101, 99, 100),
            (100, 100, 97, 99),
            (99, 105, 99, 104),
            (104, 110, 104, 109),
        ]
    )
    result = run_backtest(touched, _signal([1, 1, 1, 1, 1], stop_mult=2.0), cost_per_side=0.0)
    assert result.trades[0].exit_price == 98 and result.trades[0].exit_reason == "stop"
    assert len(result.trades) == 1 and result.equity.iloc[-1] == pytest.approx(
        0.98
    )  # no re-entry while the signal never reset

    gapped = _candles([(100, 100, 100, 100), (100, 101, 99, 100), (95, 96, 94, 95), (95, 96, 94, 95)])
    result = run_backtest(gapped, _signal([1, 1, 1, 1], stop_mult=2.0), cost_per_side=0.0)
    assert result.trades[0].exit_price == 95  # gap through the stop fills at the open, not at 98


def test_benchmarks_and_open_trade_at_end() -> None:
    candles = synthetic_candles(500)
    flat = run_backtest(candles, STRATEGIES["no_trade"](candles, None), 0.001)
    assert flat.equity.iloc[-1] == 1.0 and not flat.trades
    hold = run_backtest(candles, STRATEGIES["buy_and_hold"](candles, None), 0.0)
    assert hold.equity.iloc[-1] == pytest.approx(candles["close"].iloc[-1] / candles["open"].iloc[1])
    assert hold.trades[-1].exit_reason == "open_at_end"
    metrics = performance_metrics(hold.returns, hold.in_position, hold.trades, 8760)
    assert (
        metrics["trades"] == 0 and metrics["cagr"] is None
    )  # sample shorter than a year: no annualised return


def test_higher_costs_never_improve_a_result() -> None:
    candles = synthetic_candles(2000, seed=3)
    signal = donchian_breakout_v1(candles, {"entry_lookback": 20, "exit_lookback": 10})
    cheap = run_backtest(candles, signal, 0.0005).equity.iloc[-1]
    expensive = run_backtest(candles, signal, 0.002).equity.iloc[-1]
    assert expensive < cheap


def test_simulator_is_deterministic() -> None:
    candles = synthetic_candles(1500, seed=5)
    a = run_backtest(candles, tsmom_trend_v1(candles, None), 0.0013)
    b = run_backtest(candles, tsmom_trend_v1(candles, None), 0.0013)
    assert a.equity.equals(b.equity) and len(a.trades) == len(b.trades)


def test_volatility_scaled_signal_is_capped_and_uses_trailing_data_only() -> None:
    candles = synthetic_candles(1200, seed=31)
    base = tsmom_trend_v1(candles, None)
    targets = pd.Series(0.5, index=candles.index)
    signal = volatility_scaled_signal(candles, base, "1h", targets, lookback_bars=48)
    assert signal.target.between(0.0, 1.0).all()
    assert (signal.target <= base.target).all()
    assert (signal.target.iloc[:48] == 0.0).all()

    changed = candles.copy()
    changed.loc[700:, ["open", "high", "low", "close"]] *= 3.0
    changed_signal = volatility_scaled_signal(
        changed,
        tsmom_trend_v1(changed, None),
        "1h",
        targets,
        lookback_bars=48,
    )
    pd.testing.assert_series_equal(signal.target.iloc[:700], changed_signal.target.iloc[:700])


# ----------------------------------------------------------------------------- statistics


def test_psr_and_dsr_behave() -> None:
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, 5000)
    assert 0.05 < probabilistic_sharpe_ratio(noise) < 0.95
    good = rng.normal(0.001, 0.01, 5000)
    psr = probabilistic_sharpe_ratio(good)
    assert psr > 0.99
    assert deflated_sharpe_ratio(good, 1, 0.0) == pytest.approx(psr)
    assert deflated_sharpe_ratio(good, 200, 0.02**2) < psr  # many trials deflate the same track record
    assert expected_max_sharpe(100, 0.01) > expected_max_sharpe(10, 0.01) > 0
    assert np.isnan(probabilistic_sharpe_ratio(noise[:10]))


def test_bootstrap_interval_brackets_zero_for_noise_and_excludes_it_for_drift() -> None:
    rng = np.random.default_rng(1)
    lo, hi = block_bootstrap_ci(rng.normal(0, 0.01, 4000), n_boot=300)
    assert lo < 0 < hi
    lo, _ = block_bootstrap_ci(rng.normal(0.002, 0.01, 4000), n_boot=300)
    assert lo > 0
    assert np.isnan(block_bootstrap_ci(np.zeros(10))[0])


def test_paired_sharpe_ci_uses_same_periods_and_reports_metrics() -> None:
    rng = np.random.default_rng(71)
    base = rng.normal(0.0, 0.01, 4000)
    challenger = base + 0.001
    lo, hi = paired_sharpe_difference_ci(base, challenger, 8760, n_boot=200)
    assert lo > 0 and hi > lo
    metrics = performance_metrics(
        pd.Series(base), pd.Series(np.ones(len(base))), [], 8760, pd.Series(np.full(len(base), 0.1))
    )
    assert metrics["daily_cvar_95"] < 0
    assert metrics["turnover_per_year"] > 0
    assert metrics["max_drawdown_recovery_bars"] >= 0


def test_pbo_is_high_for_noise_and_low_for_a_genuinely_better_configuration() -> None:
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 0.01, (2000, 12))
    assert pbo_cscv(noise)["pbo"] > 0.25
    skilled = noise.copy()
    skilled[:, 0] += 0.002
    assert pbo_cscv(skilled)["pbo"] < 0.05
    assert pbo_cscv(noise[:, :2])["status"] == "not_applicable"


# ----------------------------------------------------------------------------- experiment registry / holdout


def _evaluate(tmp_path: Path, candles: pd.DataFrame, holdout_start: str, unlock: bool = False) -> dict:
    return evaluate_strategy(
        candles, symbol="BTCUSDT", interval="1h", strategy="donchian_breakout_v1", params={"entry_lookback": 48, "exit_lookback": 24},
        costs=CostConfig(), holdout_start=holdout_start, validation={"min_oos_trades": 100, "min_dsr": 0.5}, registry=ExperimentRegistry(tmp_path / "exp"),
        root=tmp_path, unlock_holdout=unlock,
    )  # fmt: skip


def test_every_run_is_logged_and_holdout_stays_locked(tmp_path: Path) -> None:
    candles = synthetic_candles(6000, seed=9)
    boundary = pd.to_datetime(int(candles["open_time"].iloc[4500]), unit="ms").strftime("%Y-%m-%d")
    first = _evaluate(tmp_path, candles, boundary)
    second = _evaluate(tmp_path, candles, boundary)
    registry = ExperimentRegistry(tmp_path / "exp")
    assert len(registry.records("backtest")) == 2
    assert first["holdout"] == {"status": "locked"} and first["data"]["holdout_bars"] > 1000
    assert first["development"]["bars"] == first["data"]["development_bars"]
    assert second["selection_bias"]["trials_including_this"] == 2
    assert first["limitations"] and "gates_provisional" in first
    unlocked = _evaluate(tmp_path, candles, boundary, unlock=True)
    assert unlocked["holdout"]["status"].startswith("UNLOCKED")
    assert registry.get(first["experiment_id"])["strategy"] == "donchian_breakout_v1"
    assert registry.get("../../etc/passwd") is None


def test_volatility_overlay_walk_forward_keeps_holdout_locked_and_unread(tmp_path: Path) -> None:
    candles = synthetic_candles(30_000, seed=19)
    boundary = pd.to_datetime(int(candles["open_time"].iloc[28_000]), unit="ms").strftime("%Y-%m-%d")
    registry = ExperimentRegistry(tmp_path / "overlay-exp")

    def evaluate(frame: pd.DataFrame) -> dict:
        return evaluate_volatility_overlay(
            frame,
            symbol="BTCUSDT",
            interval="1h",
            base_strategy="tsmom_trend_v1",
            params={"ema_fast": 24, "ema_slow": 168, "mom_lookback": 168, "atr_stop_mult": 3.0},
            costs=CostConfig(),
            holdout_start=boundary,
            validation={"min_evaluation_trades": 100, "min_dsr": 0.5},
            registry=registry,
            root=tmp_path,
            daily_rebalance=True,
            n_boot=50,
        )

    first = evaluate(candles)
    poisoned_holdout = candles.copy()
    poisoned_holdout.loc[28_000:, ["open", "high", "low", "close"]] *= 100.0
    second = evaluate(poisoned_holdout)
    assert first["holdout"] == {"status": "locked"}
    assert first["data"]["holdout_bars"] > 0
    assert first["walk_forward"]["folds"] >= 4
    assert first["data"]["oos_end"] < boundary
    assert first["overlay"]["net_total_return"] == pytest.approx(second["overlay"]["net_total_return"])
    assert first["hourly_overlay_reference"]["turnover_per_year"] > 0
    assert first["preregistered_policy_checks"]["exploratory_only_oos_previously_viewed"] is True
    assert first["params"]["rebalance_interval_hours"] == pytest.approx(24.0)
    regular_costs = first["overlay"]["modeled_cost_bps_initial_equity_by_reason"]["total"]
    stressed_costs = first["overlay_at_stressed_costs"]["modeled_cost_bps_initial_equity_by_reason"]["total"]
    assert stressed_costs["fee"] > regular_costs["fee"]
    assert stressed_costs["total"] == pytest.approx(
        stressed_costs["fee"] + stressed_costs["half_spread"] + stressed_costs["slippage"]
    )
    assert len(registry.records("volatility_overlay")) == 2


def test_overlay_family_registration_counts_every_predeclared_trial(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "family-exp")
    registry.record("backtest", {"development": {"sharpe_per_period": 0.01}})
    results = [
        {
            "symbol": symbol,
            "overlay": {"sharpe_per_period": 0.02},
            "selection_bias": {},
            "gates_provisional": {"dsr_at_least_min": True, "another_gate": True},
            "_oos_returns": np.linspace(-0.01, 0.02, 1000),
        }
        for symbol in ("BTCUSDT", "ETHUSDT")
    ]
    recorded = record_volatility_overlay_family(results, registry, min_dsr=0.5)
    assert all(result["selection_bias"]["trials_including_this"] == 3 for result in recorded)
    assert all("_oos_returns" not in result for result in recorded)
    assert all("_oos_returns" not in item for item in registry.records("volatility_overlay"))


def test_turnover_diagnostic_records_are_excluded_from_trial_count(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "diagnostic-exp")
    registry.record("backtest", {"development": {"sharpe_per_period": 0.01}})
    registry.record(
        "turnover_attribution",
        {"diagnostic_only": True, "counts_toward_dsr": False, "overlay": {"sharpe_per_period": 0.9}},
    )
    assert registry.trial_sharpes() == [0.01]


def test_unknown_strategy_and_short_history_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        evaluate_strategy(
            synthetic_candles(3000),
            symbol="X",
            interval="1h",
            strategy="nope",
            params=None,
            costs=CostConfig(),
            holdout_start="2020-01-01",
            validation={},
            registry=ExperimentRegistry(tmp_path),
            root=tmp_path,
        )
    with pytest.raises(ValueError):
        _evaluate(
            tmp_path, synthetic_candles(3000), "2000-01-01"
        )  # everything is holdout -> no development data


# ----------------------------------------------------------------------------- ML


def test_triple_barrier_labels_hand_case() -> None:
    candles = _candles(
        [
            (100, 100, 100, 100),
            (100, 103, 100, 102),
            (102, 102, 97, 98),
            (98, 99, 97, 98),
            (98, 99, 97, 98),
            (98, 99, 97, 98),
        ]
    )
    labels = ml.triple_barrier_labels(candles, pd.Series([1.0] * 6), k_up=2.0, k_dn=2.0, horizon=2)
    assert labels["label"].iloc[0] == 1.0 and labels["gross_return"].iloc[0] == pytest.approx(
        0.02
    )  # 102 touched on bar 1
    assert labels["label"].iloc[1] == 0.0  # from 102: lower barrier 100 hit on bar 2
    assert labels["label"].iloc[-2:].isna().all()  # not enough future bars -> no label
    both = _candles([(100, 100, 100, 100), (100, 103, 97, 100), (100, 100, 100, 100)])
    assert (
        ml.triple_barrier_labels(both, pd.Series([1.0] * 3), 2.0, 2.0, 1)["label"].iloc[0] == 0.0
    )  # ambiguous bar -> pessimistic


def test_walk_forward_folds_are_chronological_and_purged() -> None:
    folds = ml.walk_forward_folds(1000, train=400, test=100, purge=24)
    assert len(folds) == 6
    for train_idx, test_idx in folds:
        assert (
            train_idx.max() < test_idx.min() - 24
        )  # label windows of training rows cannot reach the test block
        assert len(np.intersect1d(train_idx, test_idx)) == 0
    assert ml.walk_forward_folds(100, 400, 100, 24) == []


def test_model_on_random_walk_is_evaluated_out_of_sample_and_predictions_carry_status(tmp_path: Path) -> None:
    candles = synthetic_candles(3500, seed=11)
    cfg = {
        "horizon_bars": 12,
        "barrier_up_atr": 2.0,
        "barrier_down_atr": 2.0,
        "train_days": 60,
        "test_days": 15,
    }
    cutoff = int(candles["open_time"].iloc[-200])
    evaluation = ml.evaluate_model(
        candles,
        symbol="BTCUSDT",
        interval="1h",
        model_name="logreg_v1",
        feature_params=SMALL,
        ml_cfg=cfg,
        cost_per_side=0.0013,
        holdout_cutoff_ms=cutoff,
        max_ece=0.05,
    )
    assert evaluation["status"] == "ok" and evaluation["folds"] >= 3
    assert evaluation["out_of_sample_status"] in {"evaluated_no_edge", "evaluated_passed_gates"}
    assert 0 <= evaluation["ece"] <= 1 and evaluation["log_loss_base_rate"] > 0
    meta = ml.train_final_model(
        candles,
        symbol="BTCUSDT",
        interval="1h",
        model_name="logreg_v1",
        feature_params=SMALL,
        ml_cfg=cfg,
        holdout_cutoff_ms=cutoff,
        evaluation=evaluation,
        models_dir=tmp_path,
    )
    assert (
        meta["trained_through"] < pd.to_datetime(cutoff, unit="ms").isoformat()
    )  # holdout rows never reach training
    prediction = ml.predict_latest(
        candles,
        symbol="BTCUSDT",
        interval="1h",
        model_name="logreg_v1",
        models_dir=tmp_path,
        use_for_decisions=False,
        data_ok=True,
    )
    assert prediction is not None and 0 <= prediction.p_up <= 1
    assert prediction.usable_for_decisions is False and prediction.expected_net_return is None
    assert (
        ml.predict_latest(
            candles,
            symbol="ETHUSDT",
            interval="1h",
            model_name="logreg_v1",
            models_dir=tmp_path,
            use_for_decisions=True,
            data_ok=True,
        )
        is None
    )
    tiny = ml.evaluate_model(
        candles.iloc[:500],
        symbol="X",
        interval="1h",
        model_name="logreg_v1",
        feature_params=SMALL,
        ml_cfg=cfg,
        cost_per_side=0.0,
        holdout_cutoff_ms=cutoff,
        max_ece=0.05,
    )
    assert tiny["status"] == "insufficient_data"

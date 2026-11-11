"""Forward paper P&L from the shadow log: equals the recomputed rules, states its limits, needs data."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tests.conftest import FakeSource, make_desk, synthetic_candles
from trader.config import load_risk_policy
from trader.quant.portfolio import PortfolioLimits, run_portfolio_backtest
from trader.quant.research import cost_component_rates
from trader.quant.strategies import STRATEGIES, StrategySignal
from trader.shadow import run_shadow_log
from trader.shadow_pnl import evaluate_shadow
from trader.timeutil import interval_ms

STEP = interval_ms("1h")
END = 1_794_000_000_000 - (1_794_000_000_000 % STEP)


def logged_desk(repo_copy: Path):  # type: ignore[no-untyped-def]
    source = FakeSource()
    full = synthetic_candles(3000, end_ms=END, seed=21, drift=0.0003)
    source.override_klines["1h"] = full
    desk = make_desk(repo_copy, source)
    run_shadow_log(desk, now_ms=int(full["close_time"].iloc[2900]) + 60_000)  # first run logs bar 2900 only
    run_shadow_log(desk, now_ms=int(full["close_time"].iloc[2999]) + 60_000)  # second run logs 2901..2999
    return desk, full


def test_shadow_pnl_equals_the_rules_recomputed_on_the_same_candles(repo_copy: Path) -> None:
    desk, full = logged_desk(repo_copy)
    report = evaluate_shadow(desk, ["tsmom_trend_v1"])
    assert report["status"] == "ok" and report["rows_missing"] == 0
    assert report["first_decision_bar"].startswith(
        pd.Timestamp(int(full["open_time"].iloc[2900]), unit="ms").strftime("%Y-%m-%dT%H")
    )

    window = full.iloc[2900:3000].reset_index(drop=True)
    recomputed = STRATEGIES["tsmom_trend_v1"](full, desk.app.strategies["tsmom_trend_v1"])
    signal = StrategySignal(
        "tsmom_trend_v1", "1", recomputed.target.iloc[2900:3000].reset_index(drop=True),
        recomputed.atr.iloc[2900:3000].reset_index(drop=True), recomputed.stop_mult, {}, pd.DataFrame(index=range(100)),
    )  # fmt: skip
    expected = run_portfolio_backtest(
        {s: window for s in desk.app.symbols}, {s: signal for s in desk.app.symbols}, interval="1h",
        limits=PortfolioLimits.from_policy(load_risk_policy(desk.root).policy),
        cost_components=cost_component_rates(desk.app.costs), start_time_ms=int(full["open_time"].iloc[2901]),
        priority=desk.app.symbols,
    )  # fmt: skip
    got = report["strategies"]["tsmom_trend_v1"]["base_costs"]
    assert got["net_total_return"] == pytest.approx(float(expected.equity[-1] - 1.0))
    assert got["closed_trades"] == len([t for t in expected.trades if t.exit_reason != "open_at_end"])
    two_x = report["strategies"]["tsmom_trend_v1"]["costs_2x"]
    assert two_x["modeled_cost_bps_initial_equity"] >= got["modeled_cost_bps_initial_equity"]
    assert (
        report["what_this_can_show"]["observed_annual_sharpe_needed_for_significance"] > 5
    )  # ~4 days of data


def test_desk_tool_warns_that_short_records_prove_nothing(repo_copy: Path) -> None:
    desk, _ = logged_desk(repo_copy)
    envelope = desk.shadow_performance()
    assert envelope["status"] == "ok" and envelope["evidence_id"].startswith("ev")
    assert any("Not evidence of profitability" in w for w in envelope["warnings"])
    assert set(envelope["data"]["strategies"]) == {"tsmom_trend_v1", "donchian_breakout_v1"}


def test_unavailable_without_shadow_rows(repo_copy: Path) -> None:
    desk = make_desk(repo_copy, FakeSource())
    assert evaluate_shadow(desk)["status"] == "unavailable"
    assert desk.shadow_performance()["status"] == "unavailable"

"""Portfolio backtester, sizing parity with the risk engine, daily bootstrap and trial accounting."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.conftest import REPO, synthetic_candles
from trader.config import load_risk_policy
from trader.domain import OrderType, SymbolFilters
from trader.quant.portfolio import (
    PortfolioLimits,
    daily_returns,
    portfolio_summary,
    run_portfolio_backtest,
    size_quantity,
)
from trader.quant.research import ExperimentRegistry, min_evaluation_trades
from trader.quant.stats import daily_bootstrap_sensitivity
from trader.quant.strategies import StrategySignal, tsmom_trend_v1
from trader.risk import compute_sizing
from trader.timeutil import utc_now

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000)
COSTS = {"fee": 0.001, "half_spread": 0.0001, "slippage": 0.0002}
LIMITS = PortfolioLimits.from_policy(load_risk_policy(REPO).policy)


def frame(opens: list[float], lows: list[float] | None = None, start: int = T0) -> pd.DataFrame:
    o = np.asarray(opens, dtype=float)
    lo = np.asarray(lows, dtype=float) if lows is not None else o * 0.999
    close = np.r_[o[1:], o[-1]]
    times = start + H * np.arange(len(o))
    return pd.DataFrame(
        {"open_time": times, "open": o, "high": np.maximum(o, close) * 1.001, "low": lo, "close": close,
         "close_time": times + H - 1}
    )  # fmt: skip


def signal(target: list[float], atr: float, stop_mult: float = 3.0) -> StrategySignal:
    n = len(target)
    return StrategySignal(
        "test",
        "1",
        pd.Series(target, dtype=float),
        pd.Series([atr] * n),
        stop_mult,
        {},
        pd.DataFrame(index=range(n)),
    )


def run(candles, signals, **kwargs):  # type: ignore[no-untyped-def]
    return run_portfolio_backtest(
        candles, signals, interval="1h", limits=kwargs.pop("limits", LIMITS), cost_components=COSTS,
        start_time_ms=kwargs.pop("start", T0 + H), **kwargs,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("equity", "free", "exposure", "entry", "stop"),
    [
        ("10000", "10000", "0", "100", "97"),  # risk budget binds
        ("10000", "10000", "0", "100", "99.5"),  # notional cap binds
        ("10000", "6000", "4500", "100", "99.5"),  # total exposure room binds
        ("10000", "300", "0", "100", "95"),  # free balance binds
        ("2534.17", "2500", "1200.5", "61234.5", "59000.25"),
    ],
)
def test_float_sizing_mirrors_decimal_risk_engine(
    equity: str, free: str, exposure: str, entry: str, stop: str
) -> None:
    filters = SymbolFilters(
        symbol="BTCUSDT", status="TRADING", base_asset="BTC", quote_asset="USDT", tick_size=Decimal("0.01"),
        min_price=Decimal("0.01"), max_price=Decimal("1000000"), step_size=Decimal("0.00000001"),
        min_qty=Decimal("0.00000001"), max_qty=Decimal("9000"), min_notional=Decimal("0"),
        order_types=["MARKET"], fetched_at=utc_now(),
    )  # fmt: skip
    exact = compute_sizing(
        equity=Decimal(equity), free_quote=Decimal(free), current_exposure=Decimal(exposure), entry=Decimal(entry),
        stop=Decimal(stop), size_scale=Decimal(1), max_risk_pct=Decimal("0.5"), max_position_notional_pct=Decimal(20),
        max_total_exposure_pct=Decimal(50), fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.0003"),
        filters=filters, order_type=OrderType.MARKET,
    )  # fmt: skip
    quantity, binding = size_quantity(
        equity=float(equity), free_quote=float(free), current_exposure=float(exposure), entry=float(entry),
        stop=float(stop), max_risk_pct=0.5, max_position_notional_pct=20, max_total_exposure_pct=50, cost_rate=0.0013,
    )  # fmt: skip
    assert binding == exact.binding_constraint
    assert 0 <= quantity - float(exact.quantity) < 1e-8 + 1e-12  # the Decimal path only adds step rounding


def test_entry_at_next_open_stop_and_cost_reconciliation() -> None:
    opens = [100, 100, 101, 102, 103, 104, 105]
    lows = [99.9, 99.9, 100.9, 101.9, 90.0, 103.9, 104.9]  # bar 4 trades through the stop
    target = [1, 1, 1, 1, 1, 1, 1]
    result = run({"A": frame(opens, lows)}, {"A": signal(target, atr=1.0)}, start=T0 + H)
    first = result.trades[0]
    assert (
        first.entry_time == T0 + H and first.entry_price == 100
    )  # decided at close of bar 0, filled at open of 1
    assert first.exit_reason == "stop" and first.exit_price == pytest.approx(97.0)
    assert first.binding_constraint == "risk_budget"  # 0.5% risk over a ~3.3% loss per unit is ~15% notional
    assert len(result.trades) == 1  # blocked after the stop until the signal goes flat
    final_equity = result.equity[-1]
    assert final_equity - 1.0 == pytest.approx(sum(t.net_pnl for t in result.trades))
    traded = sum(t.quantity * (t.entry_price + t.exit_price) for t in result.trades)
    assert sum(result.modeled_cost_by_component.values()) == pytest.approx(traded * sum(COSTS.values()))


def test_gap_below_stop_fills_at_open() -> None:
    opens = [100, 100, 101, 90, 91]
    lows = [99.9, 99.9, 100.9, 89.0, 90.9]
    result = run({"A": frame(opens, lows)}, {"A": signal([1] * 5, atr=1.0)})
    assert result.trades[0].exit_reason == "stop_gap" and result.trades[0].exit_price == 90


def test_slot_limit_uses_fixed_priority_and_reports_rejections() -> None:
    opens = [100.0] * 6
    candles = {s: frame(opens) for s in ("C", "A", "B")}
    signals = {s: signal([1] * 6, atr=1.0) for s in candles}
    result = run(candles, signals, priority=["B", "C", "A"])
    entered = sorted({t.symbol for t in result.trades})
    assert entered == ["B", "C"]  # max_open_positions = 2, taken in priority order
    assert result.rejections["max_open_positions"] > 0
    summary = portfolio_summary(result)
    assert summary["tie_break"] == "fixed priority order B > C > A"
    assert summary["max_gross_exposure"] <= 0.5 + 1e-9


def test_total_exposure_cap_shrinks_a_later_entry() -> None:
    limits = PortfolioLimits(**{**LIMITS.__dict__, "max_open_positions": 4, "max_total_exposure_pct": 50.0})
    candles = {s: frame([100.0] * 5) for s in ("A", "B", "C")}
    # 0.6% stops: the risk budget allows more than 20%, so A and B take the notional cap and C the remaining 10%
    result = run(candles, {s: signal([1] * 5, atr=0.2) for s in candles}, limits=limits)
    by_symbol = {t.symbol: t for t in result.trades}
    assert by_symbol["C"].binding_constraint == "max_total_exposure"
    assert by_symbol["C"].quantity * 100 < by_symbol["A"].quantity * 100
    # the cap applies to equity before entry costs (as in the risk engine); costs then shrink equity slightly
    assert result.gross_exposure.max() == pytest.approx(0.5, abs=1e-3)


def test_stop_distance_outside_policy_is_rejected_unless_clipped() -> None:
    candles = {"A": frame([100.0] * 5)}
    wide = {"A": signal([1] * 5, atr=5.0)}  # 3 x 5 = 15% > 10% policy maximum
    assert run(candles, wide).trades == []
    clipped = run(candles, wide, clip_stop_to_policy=True)
    assert clipped.trades and clipped.trades[0].stop_price == pytest.approx(90.0)


@pytest.mark.parametrize("price", [7000.0, 61234.57, 0.3317, 1234.5678, 99.99, 3.0, 4321.0])
def test_a_stop_clipped_to_the_policy_maximum_is_never_rejected_by_float_rounding(price: float) -> None:
    # regression 2026-11-11: (entry - entry * 0.9) / entry * 100 can evaluate to 10.000000000000002
    candles = {"A": frame([price] * 5)}
    wide = {"A": signal([1] * 5, atr=price * 0.05)}  # 3 x 5% = 15% > 10% maximum
    result = run(candles, wide, clip_stop_to_policy=True)
    assert result.rejections["stop_distance_outside_policy"] == 0
    assert result.trades and result.trades[0].stop_price == pytest.approx(price * 0.9)


def test_superseded_records_are_replaced_not_added_to_the_trial_count(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "exp")
    old = registry.record(
        "portfolio_backtest", {"role": "candidate", "interval": "1d", "portfolio": {"sharpe_per_period": 0.1}}
    )
    assert registry.trial_count() == 1 and registry.trial_count(exclude=frozenset({old})) == 0
    registry.record(
        "portfolio_backtest",
        {"role": "candidate", "interval": "1d", "portfolio": {"sharpe_per_period": 0.2}, "supersedes": [old]},
    )
    assert registry.trial_count() == 1 and registry.trial_sharpes() == pytest.approx([0.2])
    assert registry.get(old) is not None  # the defective record is kept


def test_missing_bar_neither_trades_nor_fills_and_marks_at_last_close() -> None:
    a = frame([100.0, 100.0, 101.0, 102.0, 103.0, 104.0])
    b = frame([50.0, 50.0, 50.0, 50.0, 50.0, 50.0]).drop(index=3).reset_index(drop=True)
    result = run({"A": a, "B": b}, {"A": signal([1] * 6, atr=1.0), "B": signal([1] * 5, atr=0.5)})
    assert np.all(np.isfinite(result.equity))
    assert len(result.equity) == 5  # grid is the union of bar times
    assert {t.symbol for t in result.trades} == {"A", "B"}


def test_future_prices_do_not_change_the_past() -> None:
    raw = synthetic_candles(3000, seed=11, end_ms=T0 + 3000 * H)
    shocked = raw.copy()
    shocked.loc[2500:, ["open", "high", "low", "close"]] *= 0.5
    start = int(raw["open_time"].iloc[2000])
    one = run({"A": raw}, {"A": tsmom_trend_v1(raw)}, start=start)
    two = run({"A": shocked}, {"A": tsmom_trend_v1(shocked)}, start=start)
    cut = 2500 - 2000
    assert np.allclose(one.equity[:cut], two.equity[:cut])


def test_daily_returns_and_block_sensitivity() -> None:
    raw = synthetic_candles(24 * 120, seed=12, end_ms=T0 + 24 * 120 * H)
    result = run({"A": raw}, {"A": tsmom_trend_v1(raw)}, start=int(raw["open_time"].iloc[24 * 20]))
    daily = daily_returns(result)
    assert np.prod(1 + daily.to_numpy()) == pytest.approx(result.equity[-1])
    report = daily_bootstrap_sensitivity(daily.to_numpy(), n_boot=200)
    assert list(report["by_block"]) == ["5d", "10d", "20d"]
    lows = [v["sharpe_ci_annualized"][0] for v in report["by_block"].values()]
    assert report["most_conservative_sharpe_lower"] == pytest.approx(min(lows))


def test_trial_accounting_excludes_benchmarks_and_counts_models(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "exp")
    registry.record(
        "backtest",
        {"strategy": "tsmom_trend_v1", "interval": "1h", "development": {"sharpe_per_period": 0.01}},
    )
    registry.record("backtest", {"strategy": "buy_and_hold", "development": {"sharpe_per_period": 0.05}})
    registry.record("backtest", {"strategy": "no_trade", "development": {"sharpe_per_period": None}})
    registry.record("ml_model", {"model": "logreg_v1", "status": "ok"})
    registry.record(
        "portfolio_backtest", {"role": "candidate", "interval": "1d", "portfolio": {"sharpe_per_period": 0.1}}
    )
    registry.record(
        "portfolio_backtest", {"role": "reference", "interval": "1h", "portfolio": {"sharpe_per_period": 0.2}}
    )
    assert registry.trial_count() == 3
    assert sorted(registry.trial_sharpes()) == pytest.approx([0.01, 0.1])
    rescaled = sorted(registry.trial_sharpes(target_interval="1d"))
    assert rescaled == pytest.approx([0.01 * np.sqrt(24), 0.1])  # hourly Sharpe expressed per day


def test_trade_count_gate_reads_new_and_old_names() -> None:
    assert min_evaluation_trades({"min_evaluation_trades": 30}) == 30
    assert min_evaluation_trades({"min_oos_trades": 70}) == 70
    assert min_evaluation_trades({}) == 100

"""Risk engine: hand-checked sizing and one test per rejection path."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import REPO, FakeSource
from trader.config import TradingMode, load_risk_policy
from trader.domain import OrderType, PortfolioState, Position, Quote, Side, TradeIntentRequest
from trader.risk import RiskContext, compute_sizing, validate_intent
from trader.timeutil import utc_now

FILTERS = FakeSource().symbol_filters("BTCUSDT")
D = Decimal


def sizing(**overrides):
    args = dict(
        equity=D(10000), free_quote=D(10000), current_exposure=D(0), entry=D(100), stop=D(98), size_scale=D(1), max_risk_pct=D("0.5"),
        max_position_notional_pct=D(20), max_total_exposure_pct=D(50), fee_rate=D("0.001"), slippage_rate=D("0.0002"), filters=FILTERS, order_type=OrderType.MARKET,
    )  # fmt: skip
    return compute_sizing(**{**args, **overrides})


def test_sizing_by_risk_budget_matches_hand_calculation() -> None:
    result = sizing(max_position_notional_pct=D(100), max_total_exposure_pct=D(100))
    # risk budget 50; loss/unit = 2 + 100*0.0012 + 98*0.0012 = 2.2376; 50/2.2376 = 22.3454... -> floor to 0.001
    assert result.loss_per_unit == D("2.2376") and result.risk_budget == D(50)
    assert result.quantity == D("22.345") and result.binding_constraint == "risk_budget"
    assert result.worst_case_loss <= result.risk_budget


def test_sizing_is_capped_by_notional_exposure_and_balance() -> None:
    assert sizing().binding_constraint == "max_position_notional" and sizing().quantity == D(
        20
    )  # 20% of 10k / 100
    capped = sizing(current_exposure=D(4500))
    assert capped.binding_constraint == "max_total_exposure" and capped.quantity == D(5)
    poor = sizing(free_quote=D(300))
    assert poor.binding_constraint == "free_balance" and poor.quantity * D(100) * D("1.0012") <= D(300)
    assert sizing(current_exposure=D(6000)).quantity == 0


def test_size_scale_only_reduces_and_rounding_never_exceeds_budget() -> None:
    full = sizing(max_position_notional_pct=D(100), max_total_exposure_pct=D(100))
    half = sizing(max_position_notional_pct=D(100), max_total_exposure_pct=D(100), size_scale=D("0.5"))
    assert half.quantity <= full.quantity / 2 and half.worst_case_loss <= D(25)


@pytest.mark.parametrize("entry,stop", [(D(100), D(100)), (D(100), D(101)), (D(100), D(0)), (D(0), D(-1))])
def test_sizing_rejects_invalid_stop(entry: Decimal, stop: Decimal) -> None:
    with pytest.raises(ValueError):
        sizing(entry=entry, stop=stop)


# ----------------------------------------------------------------------------- validation


def portfolio(**overrides) -> PortfolioState:
    base = dict(
        mode="PAPER", quote_asset="USDT", free_quote=D(10000), positions=[], marks={}, equity=D(10000), exposure=D(0), equity_peak=D(10000),
        realized_pnl_today=D(0), realized_pnl_week=D(0), as_of=utc_now(),
    )  # fmt: skip
    return PortfolioState(**{**base, **overrides})


def context(**overrides) -> RiskContext:
    now = utc_now()
    base = dict(
        mode=TradingMode.PAPER, loaded=load_risk_policy(REPO), now=now, intent_expires_at=now + timedelta(minutes=30),
        quote=Quote(symbol="BTCUSDT", bid=D("99.99"), ask=D("100.00"), bid_qty=D(1), ask_qty=D(1), observed_at=now),
        quote_volume_24h=D("900000000"), filters=FILTERS, portfolio=portfolio(), kill_switch_engaged=False, mode_permitted=(True, "paper"),
        other_open_intents_same_symbol=0, unknown_intents_same_symbol=0, last_loss_close=None, candle_staleness_bars=0.5,
        fee_rate=D("0.001"), slippage_rate=D("0.0002"),
    )  # fmt: skip
    return RiskContext(**{**base, **overrides})


def request(**overrides) -> TradeIntentRequest:
    base = dict(
        decision_id="dec_1",
        symbol="BTCUSDT",
        side=Side.BUY,
        stop_price=D(98),
        rationale="unit test request with enough text",
        idempotency_key="key-000001",
    )
    return TradeIntentRequest(**{**base, **overrides})


def failed(report) -> set[str]:
    return {c.name for c in report.failures}


def test_valid_long_is_approved_with_server_side_quantity() -> None:
    report = validate_intent("int_1", request(), context(), "creation")
    assert report.approved and report.sizing is not None and report.sizing.quantity == D(20)
    assert report.policy_version == "0.1.0" and len(report.policy_sha256) == 64


def position(symbol: str = "BTCUSDT", qty: str = "5") -> Position:
    return Position(
        symbol=symbol,
        quantity=D(qty),
        avg_entry=D(100),
        stop_price=D(98),
        take_profit=None,
        opened_at=utc_now(),
        intent_id="int_0",
        last_checked_ms=0,
    )


REJECTIONS = [
    ("kill_switch_clear", dict(kill_switch_engaged=True), {}),
    ("mode_permitted", dict(mode_permitted=(False, "live_disabled")), {}),
    ("symbol_allowed", {}, dict(symbol="DOGEUSDT")),
    ("quote_available", dict(quote=None), {}),
    (
        "quote_fresh",
        dict(
            quote=Quote(
                symbol="BTCUSDT",
                bid=D("99.99"),
                ask=D(100),
                bid_qty=D(1),
                ask_qty=D(1),
                observed_at=utc_now() - timedelta(seconds=120),
            )
        ),
        {},
    ),
    (
        "spread_within_limit",
        dict(
            quote=Quote(
                symbol="BTCUSDT", bid=D("99.5"), ask=D(100), bid_qty=D(1), ask_qty=D(1), observed_at=utc_now()
            )
        ),
        {},
    ),
    ("exchange_filters_available", dict(filters=None), {}),
    ("portfolio_available", dict(portfolio=None), {}),
    ("liquidity_sufficient", dict(quote_volume_24h=D(1000)), {}),
    ("liquidity_sufficient", dict(quote_volume_24h=None), {}),
    ("candles_fresh", dict(candle_staleness_bars=5.0), {}),
    ("candles_fresh", dict(candle_staleness_bars=None), {}),
    ("stop_loss_present", {}, dict(stop_price=None)),
    ("stop_distance_within_limits", {}, dict(stop_price=D("99.9"))),
    ("stop_distance_within_limits", {}, dict(stop_price=D(50))),
    ("stop_distance_within_limits", {}, dict(stop_price=D(105))),
    ("take_profit_above_entry", {}, dict(take_profit=D(99))),
    ("limit_is_marketable", {}, dict(order_type=OrderType.LIMIT, limit_price=D(90))),
    ("no_duplicate_open_intent", dict(other_open_intents_same_symbol=1), {}),
    ("no_unresolved_order_state", dict(unknown_intents_same_symbol=1), {}),
    ("exchange_state_reconciled", dict(reconciliation=(False, "mismatch")), {}),
    ("intent_not_expired", dict(intent_expires_at=utc_now() - timedelta(seconds=1)), {}),
    (
        "no_existing_position",
        dict(portfolio=portfolio(positions=[position()], marks={"BTCUSDT": D(100)}, exposure=D(500))),
        {},
    ),
    (
        "max_open_positions",
        dict(
            portfolio=portfolio(
                positions=[position("ETHUSDT"), position("SOLUSDT")],
                marks={"ETHUSDT": D(100), "SOLUSDT": D(100)},
                exposure=D(1000),
            )
        ),
        {},
    ),
    ("daily_loss_limit", dict(portfolio=portfolio(realized_pnl_today=D(-250))), {}),
    ("weekly_loss_limit", dict(portfolio=portfolio(realized_pnl_week=D(-600))), {}),
    ("drawdown_within_limit", dict(portfolio=portfolio(equity=D(8900), free_quote=D(8900))), {}),
    ("cooldown_after_loss", dict(last_loss_close=utc_now() - timedelta(minutes=5)), {}),
    ("position_sizing", dict(portfolio=portfolio(free_quote=D("0.01"))), {}),
    ("expected_slippage_within_limit", dict(slippage_rate=D("0.01")), {}),
]


@pytest.mark.parametrize(
    "check,ctx_overrides,req_overrides", REJECTIONS, ids=[f"{i}-{r[0]}" for i, r in enumerate(REJECTIONS)]
)
def test_each_violation_rejects(check: str, ctx_overrides: dict, req_overrides: dict) -> None:
    report = validate_intent("int_1", request(**req_overrides), context(**ctx_overrides), "creation")
    assert not report.approved
    assert check in failed(report), failed(report)


def test_execution_quantity_can_never_exceed_the_approved_quantity() -> None:
    report = validate_intent("int_1", request(), context(approved_quantity_cap=D("3.5")), "execution")
    assert (
        report.approved
        and report.sizing.quantity == D("3.5")
        and report.sizing.binding_constraint == "approved_quantity"
    )
    smaller = validate_intent("int_1", request(), context(approved_quantity_cap=D(500)), "execution")
    assert smaller.sizing.quantity == D(20)  # a larger approval does not raise the fresh risk limit


def test_sell_only_reduces_and_is_allowed_under_kill_switch() -> None:
    sell = request(side=Side.SELL, stop_price=None)
    rejected = validate_intent("int_1", sell, context(), "creation")
    assert "position_exists_to_reduce" in failed(rejected)
    held = portfolio(positions=[position(qty="5")], marks={"BTCUSDT": D("99.99")}, exposure=D(500))
    approved = validate_intent("int_1", sell, context(portfolio=held, kill_switch_engaged=True), "creation")
    assert approved.approved and approved.sizing.quantity == D(5)  # never more than the position: no shorting
    half = validate_intent(
        "int_1",
        request(side=Side.SELL, stop_price=None, size_scale=D("0.5")),
        context(portfolio=held),
        "creation",
    )
    assert half.sizing.quantity == D("2.5")


def test_a_check_that_raises_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.risk as risk

    monkeypatch.setattr(risk, "compute_sizing", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    report = validate_intent("int_1", request(), context(), "creation")
    assert not report.approved and "position_sizing" in failed(report)

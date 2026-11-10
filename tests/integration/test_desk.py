"""End-to-end and failure-path tests through TradingDesk with a fake market source (no network)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.conftest import FakeSource, decision_payload, make_desk, open_long, synthetic_candles
from trader.config import TradingMode
from trader.domain import Fill, OrderResult, OrderType, Side
from trader.exchanges.binance.client import BinanceAPIError, OrderStateUnknown
from trader.timeutil import iso, utc_now

D = Decimal


def failed_names(envelope: dict) -> set[str]:
    return {c["name"] for c in envelope["data"]["validation"]["failed_checks"]}


# ----------------------------------------------------------------------------- happy path


def test_full_trade_lifecycle_in_paper(desk, source: FakeSource) -> None:
    status = desk.system_status()
    assert status["data"]["trading_mode"] == "PAPER" and status["data"]["live"]["unlocked"] is False
    assert any("No strategy has passed" in w for w in status["warnings"])

    intent = open_long(desk, take_profit="104.00")
    assert intent["status"] == "ok", intent
    intent_id = intent["data"]["intent"]["intent_id"]
    assert intent["data"]["intent"]["status"] == "PENDING_APPROVAL"
    quantity = D(intent["data"]["intent"]["quantity"])
    assert quantity == D("19.998")  # 20% notional cap: 2000 / 100.01 = 19.9980..., floored to the 0.001 step

    executed = desk.execute_approved_trade(intent_id)
    assert executed["status"] == "ok" and executed["data"]["intent_status"] == "FILLED"
    assert D(executed["data"]["order"]["avg_price"]) == D("100.01") * D("1.0002")
    assert executed["data"]["execution_quality"]["realized_slippage_bps"] == "2"

    replay = desk.execute_approved_trade(intent_id)  # duplicate tool call
    assert replay["data"]["replayed"] is True
    assert len(desk.db.query("SELECT * FROM orders")) == 1 and len(desk.db.query("SELECT * FROM fills")) == 1

    positions = desk.open_positions()["data"]["positions"]
    assert len(positions) == 1 and D(positions[0]["stop_price"]) == D(98)
    account = desk.account_state()["data"]
    assert D(account["equity"]) < D(10000) and account["open_positions"] == 1

    # price collapses through the stop on a later 1m bar -> the paper monitor settles it at the stop minus slippage
    bars = synthetic_candles(
        5, "1m", end_ms=int(utc_now().timestamp() * 1000) + 6 * 60_000, start_price=100.0
    )
    bars["open_time"] += 2 * 60_000
    bars["close_time"] += 2 * 60_000
    bars[["open", "high", "low", "close"]] = [
        [100.0, 100.2, 99.5, 99.8],
        [99.8, 99.9, 97.0, 97.5],
        [97.5, 98, 97, 97.2],
        [97.2, 98, 97, 97.5],
        [97.5, 98, 97, 97.6],
    ]
    source.override_klines["1m"] = bars
    source.bid, source.ask = D("97.50"), D("97.51")
    desk.db.execute("UPDATE positions SET last_checked_ms=?", (int(bars["open_time"].iloc[0]) - 1,))
    import trader.execution as execution

    real_now = execution.utc_now
    execution.utc_now = lambda: (
        real_now() + timedelta(minutes=10)
    )  # the bars above are "in the past" for the monitor
    try:
        settled = desk.execution.monitor_positions()
    finally:
        execution.utc_now = real_now
    assert settled and settled[0]["exit_reason"] == "stop"
    assert D(settled[0]["avg_exit"]) == D(98) * (1 - D("0.0002"))
    assert D(settled[0]["realized_pnl"]) < 0
    assert desk.open_positions()["data"]["positions"] == []

    case = desk.trade_case(intent_id)["data"]
    assert case["outcome"]["result"] == "loss" and case["decision_report"]["decision"] == "LONG_CANDIDATE"
    assert {e["event"] for e in case["journal"]} >= {
        "intent_created",
        "approval_recorded",
        "execution_validation",
        "order_result",
        "position_opened",
        "position_closed",
    }
    assert (
        case["approvals"][0]["method"] == "claude_code_permission_prompt"
        and case["approvals"][0]["consumed_at"]
    )

    review = {
        "intent_id": intent_id, "decision_quality": "acceptable", "process_quality": "good", "data_quality": "good", "model_quality": "not_assessable",
        "execution_quality": "good", "risk_quality": "good", "regime_fit": "poor",
        "what_was_known_at_decision_time": "The synthetic snapshot showed nothing conclusive at decision time.",
        "counterfactual": "Waiting for a confirmed regime would have avoided the entry.",
        "lesson": {"title": "Entries in range regimes stop out", "claim": "Long entries taken while the regime label is range tend to hit the stop.", "scope": "BTCUSDT 1h range regime"},
    }  # fmt: skip
    policy_before = (desk.root / "configs" / "risk_policy.yaml").read_bytes()
    submitted = desk.submit_post_trade_review(review)
    assert submitted["status"] == "ok" and submitted["data"]["lesson"]["status"] == "candidate"
    assert (
        desk.root / "configs" / "risk_policy.yaml"
    ).read_bytes() == policy_before  # a loss changes no policy
    lessons = desk.pending_lessons()["data"]["lessons"]
    assert len(lessons) == 1 and lessons[0]["citable_as_rule"] is False
    assert desk.search_knowledge("range regimes", min_status="reviewed")["data"]["results"] == []

    # cooldown after the losing close blocks an immediate re-entry in the same symbol
    again = open_long(desk, stop="95.60", key="test-key-0002")
    assert "cooldown_after_loss" in failed_names(again)
    assert desk.db.journal_verify()[0]


def test_reduce_only_sell_closes_position_and_records_pnl(desk, source: FakeSource) -> None:
    intent = open_long(desk)
    desk.execute_approved_trade(intent["data"]["intent"]["intent_id"])
    source.bid, source.ask = D("103.00"), D("103.01")
    decision = desk.record_decision_report({**decision_payload(desk), "decision": "EXIT_REVIEW"})
    sell = desk.create_trade_intent(
        decision_id=decision["data"]["decision_id"],
        symbol="BTCUSDT",
        side="SELL",
        stop_price=None,
        rationale="take profit on the test position now",
        idempotency_key="sell-key-0001",
    )
    assert sell["status"] == "ok"
    result = desk.execute_approved_trade(sell["data"]["intent"]["intent_id"])
    assert result["status"] == "ok" and D(result["data"]["closed"]["realized_pnl"]) > 0
    assert result["data"]["closed"]["remaining"] == "0"
    assert desk.execution.positions() == []
    naked = desk.create_trade_intent(
        decision_id=decision["data"]["decision_id"],
        symbol="BTCUSDT",
        side="SELL",
        stop_price=None,
        rationale="attempt to sell with no position must fail",
        idempotency_key="sell-key-0002",
    )
    assert "position_exists_to_reduce" in failed_names(naked)  # no shorting


# ----------------------------------------------------------------------------- evidence rules


def test_decision_report_evidence_rules(desk) -> None:
    good = decision_payload(desk)
    fake = {
        **good,
        "quantitative_evidence": [
            {"kind": "observed_fact", "statement": "RSI is 99 trust me", "evidence_ids": ["ev_invented"]}
        ],
    }
    assert "not issued by this server" in str(desk.record_decision_report(fake)["data"]["problems"])
    uncited = {
        **good,
        "quantitative_evidence": [{"kind": "observed_fact", "statement": "RSI is 99 trust me"}],
    }
    assert desk.record_decision_report(uncited)["status"] == "rejected"
    forecast = {**good, "expected_net_return": 0.05}
    assert "expected_net_return must be null" in str(
        desk.record_decision_report(forecast)["data"]["problems"]
    )
    news = {**good, "news_evidence": [{"kind": "source_claim", "statement": "An exchange was hacked"}]}
    assert desk.record_decision_report(news)["status"] == "rejected"
    cited_news = {
        **good,
        "news_evidence": [
            {
                "kind": "source_claim",
                "statement": "An exchange was hacked",
                "source_urls": ["https://reuters.com/x"],
                "published_at": "unknown",
            }
        ],
    }
    assert desk.record_decision_report(cited_news)["status"] == "ok"
    assert desk.record_decision_report({**good, "knowledge_references": ["kb-nope"]})["status"] == "rejected"
    assert desk.record_decision_report({**good, "symbol": "DOGEUSDT"})["status"] == "rejected"
    no_trade = {
        "as_of": utc_now().isoformat(),
        "symbol": "ETHUSDT",
        "horizon": "24h",
        "decision": "NO_TRADE",
        "reason_for_no_trade": "nothing validated is signalling",
    }
    assert desk.record_decision_report(no_trade)["status"] == "ok"


def test_intent_requires_a_recorded_decision(desk) -> None:
    result = desk.create_trade_intent(
        decision_id="dec_missing",
        symbol="BTCUSDT",
        side="BUY",
        stop_price="98",
        rationale="no decision report exists for this",
        idempotency_key="k-00000001",
    )
    assert result["status"] == "rejected" and "decision" in result["data"]["reason"]


# ----------------------------------------------------------------------------- failure paths


def test_idempotency_key_replays_instead_of_duplicating(desk) -> None:
    first = open_long(desk, key="same-key-0001")
    second = desk.create_trade_intent(
        decision_id=first["data"]["intent"]["request"]["decision_id"],
        symbol="BTCUSDT",
        side="BUY",
        stop_price="98.00",
        rationale="integration test long entry with a two percent stop",
        idempotency_key="same-key-0001",
    )
    assert (
        second["data"]["replayed"] is True
        and second["data"]["intent"]["intent_id"] == first["data"]["intent"]["intent_id"]
    )
    assert len(desk.db.query("SELECT * FROM intents")) == 1
    other = open_long(desk, key="other-key-0001")
    assert "no_duplicate_open_intent" in failed_names(other)


def test_stale_quote_and_outage_block_new_intents(desk, source: FakeSource) -> None:
    source.quote_age_seconds = 120
    assert "quote_fresh" in failed_names(open_long(desk, key="stale-key-001"))
    source.quote_age_seconds = 0
    decision = desk.record_decision_report(decision_payload(desk))
    source.fail_quote = True
    blocked = desk.create_trade_intent(
        decision_id=decision["data"]["decision_id"],
        symbol="BTCUSDT",
        side="BUY",
        stop_price="98",
        rationale="quote source is down so this must fail",
        idempotency_key="outage-key-01",
    )
    assert blocked["status"] == "rejected" and "quote_available" in failed_names(blocked)
    assert desk.market_snapshot("BTCUSDT")["status"] == "unavailable"


def test_stale_candles_block_new_intents(desk, source: FakeSource) -> None:
    old = synthetic_candles(3000, "1h", end_ms=int(utc_now().timestamp() * 1000) - 10 * 3_600_000)
    source.override_klines["1h"] = old
    snapshot = desk.market_snapshot("BTCUSDT")
    assert snapshot["status"] == "stale" and any("stale" in w for w in snapshot["warnings"])
    assert "candles_fresh" in failed_names(open_long(desk, key="oldbars-key-1"))


def test_revalidation_at_execution_time_rejects_when_conditions_changed(desk, source: FakeSource) -> None:
    intent = open_long(desk)
    source.bid, source.ask = D("99.00"), D("100.01")  # spread blows out after the intent was approved
    result = desk.execute_approved_trade(intent["data"]["intent"]["intent_id"])
    assert result["status"] == "rejected" and "spread_within_limit" in str(result["data"]["failed_checks"])
    assert desk.db.query("SELECT * FROM orders") == []


def test_kill_switch_blocks_entries_and_cannot_be_released_via_desk(desk) -> None:
    pending = open_long(desk, key="pending-key-01")
    state = desk.activate_kill_switch("test", cancel_pending_entries=True)["data"]
    assert state["engaged"] and state["cancelled_pending_entry_intents"] == [
        pending["data"]["intent"]["intent_id"]
    ]
    assert "kill_switch_clear" in failed_names(open_long(desk, key="after-kill-01"))
    assert not hasattr(desk, "release_kill_switch") and not hasattr(desk, "approve")
    assert desk.execute_approved_trade(pending["data"]["intent"]["intent_id"])["status"] == "rejected"


def test_expired_intent_cannot_execute(desk) -> None:
    intent = open_long(desk)
    intent_id = intent["data"]["intent"]["intent_id"]
    desk.db.execute(
        "UPDATE intents SET expires_at=? WHERE intent_id=?",
        (iso(utc_now() - timedelta(minutes=1)), intent_id),
    )
    result = desk.execute_approved_trade(intent_id)
    assert result["status"] == "rejected" and "expired" in result["data"]["reason"]
    assert desk.trade_intent(intent_id)["data"]["intent"]["status"] == "EXPIRED"


def test_unreadable_or_tampered_policy_fails_closed(desk) -> None:
    intent = open_long(desk)
    path = desk.root / "configs" / "risk_policy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('max_leverage: "1"', 'max_leverage: "10"'), encoding="utf-8"
    )
    assert desk.execute_approved_trade(intent["data"]["intent"]["intent_id"])["status"] in {
        "rejected",
        "error",
    }
    assert desk.db.query("SELECT * FROM orders") == []
    assert desk.risk_state()["status"] == "unavailable"
    assert desk.system_status()["data"]["risk_policy"]["status"] == "UNREADABLE"
    blocked = open_long(desk, key="policy-broken1")
    assert blocked["status"] in {"rejected", "error"} and "intent" not in blocked["data"]
    assert len(desk.db.query("SELECT * FROM intents")) == 1  # nothing new was stored


def test_insufficient_balance_is_rejected_by_sizing(desk) -> None:
    desk.execution.broker()
    desk.db.execute("UPDATE paper_balances SET free='0.50'")
    assert "position_sizing" in failed_names(open_long(desk)) or "exchange_filters_satisfied" in failed_names(
        open_long(desk, key="poor-key-0002")
    )


def test_symbol_outside_universe_is_refused_everywhere(desk) -> None:
    with pytest.raises(ValueError):
        desk.market_snapshot("DOGEUSDT")
    assert desk.calculate_position_risk("DOGEUSDT", "1")["status"] == "rejected"


def test_cancel_only_affects_pending_intents(desk) -> None:
    intent = open_long(desk)
    intent_id = intent["data"]["intent"]["intent_id"]
    assert desk.cancel_trade_intent(intent_id, "changed my mind")["status"] == "ok"
    assert desk.cancel_trade_intent(intent_id, "again")["status"] == "rejected"
    assert desk.execute_approved_trade(intent_id)["status"] == "rejected"


# ----------------------------------------------------------------------------- demo / live gates


class ScriptedBroker:
    """Fake exchange broker: scripted execute() outcomes, records every send."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.sent: list[str] = []
        self.orders: dict[str, OrderResult] = {}
        self.stops: list[dict] = []
        self.lost_order: OrderResult | None = None

    def free_quote(self, quote_asset: str) -> Decimal:
        return D(10000)

    def asset_total(self, asset: str) -> Decimal:
        return D(1000)

    def filled(self, client_order_id: str, quantity: Decimal, side: Side = Side.BUY) -> OrderResult:
        return OrderResult(
            client_order_id=client_order_id, exchange_order_id="42", symbol="BTCUSDT", side=side, order_type=OrderType.MARKET, status="FILLED",
            requested_qty=quantity, executed_qty=quantity, avg_price=D("100.02"), fills=[Fill(price=D("100.02"), quantity=quantity, fee=D("0.001"), fee_asset="BTC", ts=utc_now())],
        )  # fmt: skip

    def execute(
        self, *, symbol, side, order_type, quantity, limit_price, client_order_id, quote
    ) -> OrderResult:
        self.sent.append(client_order_id)
        outcome = self.outcomes.pop(0)
        if outcome == "fill":
            self.orders[client_order_id] = self.filled(client_order_id, quantity, side)
            return self.orders[client_order_id]
        if outcome == "lost_but_filled":
            self.lost_order = self.filled(client_order_id, quantity, side)
            raise OrderStateUnknown("timeout")
        if outcome == "lost_never_arrived":
            raise OrderStateUnknown("timeout")
        raise outcome

    def find_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        if self.lost_order is not None and self.lost_order.client_order_id == client_order_id:
            return self.lost_order
        return self.orders.get(client_order_id)

    def place_protective_stop(
        self, *, symbol, quantity, stop_price, filters, client_order_id
    ) -> OrderResult | None:
        self.stops.append({"quantity": quantity, "stop": stop_price, "id": client_order_id})
        return None

    def cancel(self, symbol: str, client_order_id: str) -> None:
        pass


def demo_desk(root: Path, source: FakeSource, outcomes: list):
    broker = ScriptedBroker(outcomes)
    desk = make_desk(
        root,
        source,
        TradingMode.BINANCE_DEMO,
        broker=broker,
        binance_demo_api_key=SecretStr("k"),
        binance_demo_api_secret=SecretStr("s"),
    )
    return desk, broker


def test_demo_requires_cli_approval_which_is_single_use_and_parameter_bound(
    repo_copy: Path, source: FakeSource
) -> None:
    desk, broker = demo_desk(repo_copy, source, ["fill"])
    intent = open_long(desk)
    intent_id = intent["data"]["intent"]["intent_id"]
    assert "python -m trader approve" in intent["data"]["next_step"]
    waiting = desk.execute_approved_trade(intent_id)
    assert (
        waiting["status"] == "rejected"
        and waiting["data"]["reason"] == "awaiting_cli_approval"
        and broker.sent == []
    )

    approval = desk.execution.record_cli_approval(
        intent_id
    )  # what the TTY-gated CLI does after the operator confirms
    # an approval for different parameters must not be usable
    desk.db.execute("UPDATE intents SET quantity='999' WHERE intent_id=?", (intent_id,))
    assert desk.execute_approved_trade(intent_id)["data"]["reason"] == "awaiting_cli_approval"
    desk.db.execute(
        "UPDATE intents SET quantity=? WHERE intent_id=?", (intent["data"]["intent"]["quantity"], intent_id)
    )

    done = desk.execute_approved_trade(intent_id)
    assert done["status"] == "ok" and broker.sent == [intent["data"]["intent"]["client_order_id"]]
    assert done["data"]["protective_stop"]["status"] == "placed" and broker.stops[0]["stop"] == D("98.00")
    assert D(desk.execution.positions()[0].quantity) == D(intent["data"]["intent"]["quantity"]) - D(
        "0.001"
    )  # fee paid in BTC
    assert desk.db.query("SELECT consumed_at FROM approvals WHERE approval_id=?", (approval["approval_id"],))[
        0
    ]["consumed_at"]
    assert desk.execute_approved_trade(intent_id)["data"]["replayed"] is True and len(broker.sent) == 1


def test_expired_approval_is_not_accepted(repo_copy: Path, source: FakeSource) -> None:
    desk, broker = demo_desk(repo_copy, source, ["fill"])
    intent_id = open_long(desk)["data"]["intent"]["intent_id"]
    approval = desk.execution.record_cli_approval(intent_id)
    desk.db.execute(
        "UPDATE approvals SET expires_at=? WHERE approval_id=?",
        (iso(utc_now() - timedelta(seconds=5)), approval["approval_id"]),
    )
    assert (
        desk.execute_approved_trade(intent_id)["data"]["reason"] == "awaiting_cli_approval"
        and broker.sent == []
    )


def test_unknown_order_state_is_reconciled_never_resent(repo_copy: Path, source: FakeSource) -> None:
    desk, broker = demo_desk(repo_copy, source, ["lost_but_filled"])
    intent_id = open_long(desk)["data"]["intent"]["intent_id"]
    desk.execution.record_cli_approval(intent_id)
    result = desk.execute_approved_trade(intent_id)
    # the timeout hid a fill; reconciliation by client order id found it without sending anything again
    assert len(broker.sent) == 1
    assert result["data"]["reconciliation"] == "resolved"
    assert desk.trade_intent(intent_id)["data"]["intent"]["status"] == "FILLED"
    assert len(desk.execution.positions()) == 1
    assert desk.execute_approved_trade(intent_id)["data"]["replayed"] is True and len(broker.sent) == 1


def test_unknown_order_that_never_arrived_blocks_symbol_until_resolved(
    repo_copy: Path, source: FakeSource
) -> None:
    desk, broker = demo_desk(repo_copy, source, ["lost_never_arrived"])
    intent_id = open_long(desk)["data"]["intent"]["intent_id"]
    desk.execution.record_cli_approval(intent_id)
    result = desk.execute_approved_trade(intent_id)
    assert (
        result["status"] == "error"
        and result["data"]["intent_status"] == "UNKNOWN"
        and result["data"]["reconciliation"] == "still_unknown"
    )
    assert "no_unresolved_order_state" in failed_names(open_long(desk, key="while-unknown1"))
    assert desk.execute_approved_trade(intent_id)["status"] == "rejected" and len(broker.sent) == 1
    # 30s later the exchange still has no such order -> definite answer -> FAILED, symbol unblocked
    rows = desk.db.query("SELECT seq FROM journal WHERE event_type='order_state_unknown'")
    assert rows
    import trader.execution as execution

    real_now = execution.utc_now
    execution.utc_now = lambda: real_now() + timedelta(seconds=45)
    try:
        assert desk.execution.reconcile(intent_id)["reconciliation"] == "resolved_not_on_exchange"
    finally:
        execution.utc_now = real_now
    assert (
        desk.trade_intent(intent_id)["data"]["intent"]["status"] == "FAILED"
        and desk.execution.positions() == []
    )


def test_definite_exchange_rejection_marks_intent_failed(repo_copy: Path, source: FakeSource) -> None:
    desk, broker = demo_desk(repo_copy, source, [BinanceAPIError(400, -2010, "insufficient balance")])
    intent_id = open_long(desk)["data"]["intent"]["intent_id"]
    desk.execution.record_cli_approval(intent_id)
    result = desk.execute_approved_trade(intent_id)
    assert (
        result["status"] == "rejected"
        and result["data"]["intent_status"] == "FAILED"
        and desk.execution.positions() == []
    )


def test_local_position_must_match_exchange_balance(repo_copy: Path, source: FakeSource) -> None:
    desk, broker = demo_desk(repo_copy, source, ["fill"])
    intent_id = open_long(desk)["data"]["intent"]["intent_id"]
    desk.execution.record_cli_approval(intent_id)
    desk.execute_approved_trade(intent_id)
    broker.asset_total = lambda asset: D(0)  # exchange says we hold nothing
    blocked = open_long(desk, symbol="ETHUSDT", key="mismatch-key1")
    assert "exchange_state_reconciled" in failed_names(blocked)


def test_demo_without_credentials_is_not_permitted(repo_copy: Path, source: FakeSource) -> None:
    desk = make_desk(repo_copy, source, TradingMode.BINANCE_DEMO)
    assert desk.system_status()["data"]["execution"]["permitted"] is False
    assert desk.account_state()["status"] == "unavailable"
    assert open_long(desk)["status"] == "rejected"


def test_live_stays_locked_until_every_condition_holds(repo_copy: Path, source: FakeSource) -> None:
    # credentials + mode alone are not enough
    desk = make_desk(
        repo_copy,
        source,
        TradingMode.LIVE,
        binance_live_api_key=SecretStr("k"),
        binance_live_api_secret=SecretStr("s"),
    )
    ok, reasons = desk.execution.live_gate()
    assert not ok and len(reasons) == 3  # flag, policy status, unlock
    assert "live_disabled" in desk.execution.mode_permitted()[1]
    assert "mode_permitted" in failed_names(open_long(desk))

    desk = make_desk(
        repo_copy,
        source,
        TradingMode.LIVE,
        live_trading_enabled=True,
        binance_live_api_key=SecretStr("k"),
        binance_live_api_secret=SecretStr("s"),
    )
    assert len(desk.execution.live_gate()[1]) == 2
    policy = repo_copy / "configs" / "risk_policy.yaml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace("status: provisional", "status: approved"),
        encoding="utf-8",
    )
    assert len(desk.execution.live_gate()[1]) == 1
    unlock = repo_copy / "data" / "live_unlock.json"
    unlock.write_text(json.dumps({"expires_at": iso(utc_now() - timedelta(minutes=1))}), encoding="utf-8")
    assert "expired" in desk.execution.live_gate()[1][0]
    unlock.write_text(json.dumps({"expires_at": iso(utc_now() + timedelta(minutes=5))}), encoding="utf-8")
    assert desk.execution.live_gate() == (True, [])
    # and a paper-mode process is never "live" even with all of that in place
    assert make_desk(repo_copy, source, TradingMode.PAPER).execution.live_gate()[0] is False


def test_get_open_positions_is_a_pure_read(desk, source: FakeSource) -> None:
    """A stop that triggered is only settled by sync_positions, never by the read tool."""
    intent = open_long(desk)
    desk.execute_approved_trade(intent["data"]["intent"]["intent_id"])
    bars = synthetic_candles(5, "1m", end_ms=int(utc_now().timestamp() * 1000) - 60_000, start_price=100.0)
    bars[["open", "high", "low", "close"]] = [
        [100.0, 100.2, 99.5, 99.8],
        [99.8, 99.9, 90.0, 91.0],
        [91, 92, 90, 91],
        [91, 92, 90, 91],
        [91, 92, 90, 91],
    ]
    source.override_klines["1m"] = bars
    desk.db.execute("UPDATE positions SET last_checked_ms=?", (int(bars["open_time"].iloc[0]) - 1,))

    def snapshot() -> tuple:
        return (
            desk.db.query("SELECT * FROM positions"),
            desk.db.query("SELECT COUNT(*) AS n FROM closed_trades"),
            desk.db.query("SELECT COUNT(*) AS n FROM journal"),
            desk.db.query("SELECT * FROM paper_balances"),
        )

    before = snapshot()
    view = desk.open_positions()
    assert len(view["data"]["positions"]) == 1 and "settled" not in view["data"]
    assert snapshot() == before
    synced = desk.sync_positions()
    assert synced["data"]["settled"] and synced["data"]["settled"][0]["exit_reason"] == "stop"
    assert synced["data"]["positions"] == []

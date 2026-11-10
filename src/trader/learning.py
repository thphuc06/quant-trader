"""Post-trade learning: trade cases from the journal, excursion statistics, reviews, candidate lessons.

Nothing here changes strategy, policy, models or skills. The only outputs are journal events and
``candidate`` lesson files that need validation and a human promotion.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from trader.domain import Strict, jsonable
from trader.knowledge import KnowledgeStore
from trader.marketdata import MarketData
from trader.storage.candles import klines_to_frame
from trader.storage.db import Database
from trader.timeutil import parse_iso, to_ms, utc_now

Rating = Literal["good", "acceptable", "poor", "not_assessable"]


class LessonDraft(Strict):
    title: str = Field(min_length=8, max_length=120)
    claim: str = Field(min_length=20, max_length=1500)
    scope: str = Field(
        min_length=5, max_length=300, description="asset / regime / horizon where the claim might apply"
    )
    contradicting_cases: list[str] = Field(default_factory=list)


class PostTradeReview(Strict):
    """Process and outcome are rated separately: a loss is not automatically a bad decision."""

    intent_id: str
    decision_quality: Rating
    process_quality: Rating
    data_quality: Rating
    model_quality: Rating
    execution_quality: Rating
    risk_quality: Rating
    regime_fit: Rating
    what_was_known_at_decision_time: str = Field(min_length=20, max_length=2000)
    counterfactual: str = Field(min_length=10, max_length=1500)
    notes: str = Field(default="", max_length=3000)
    lesson: LessonDraft | None = None


def excursions(
    market: MarketData,
    symbol: str,
    entry: Decimal,
    stop: Decimal | None,
    opened_at: str,
    closed_at: str | None,
) -> dict[str, Any]:
    """Maximum favourable / adverse excursion from 1h bars between open and close (or now)."""
    start = to_ms(parse_iso(opened_at))
    end = to_ms(parse_iso(closed_at)) if closed_at else to_ms(utc_now())
    try:
        frame = klines_to_frame(
            market.source.klines(symbol, "1h", start_ms=start - 3_600_000, end_ms=end, limit=1000)
        )
    except Exception as exc:
        return {"status": "unavailable", "detail": type(exc).__name__}
    if frame.empty:
        return {"status": "unavailable", "detail": "no bars in the holding window"}
    e = float(entry)
    mfe, mae = float(frame["high"].max()) / e - 1.0, float(frame["low"].min()) / e - 1.0
    out: dict[str, Any] = {
        "status": "ok",
        "resolution": "1h bars; the entry and exit bars are included whole, so values are approximate",
        "bars": len(frame),
        "mfe_pct": round(mfe * 100, 4),
        "mae_pct": round(mae * 100, 4),
    }
    if stop is not None and entry > stop:
        risk = float((entry - stop) / entry)
        out["mfe_r"], out["mae_r"] = round(mfe / risk, 3), round(mae / risk, 3)
    return out


def trade_case(db: Database, market: MarketData, intent_id: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
    if not rows:
        return None
    intent = rows[0]
    request = json.loads(intent["request_json"])
    decision_rows = db.query(
        "SELECT payload_json FROM decisions WHERE decision_id=?", (intent["decision_id"],)
    )
    closed = db.query("SELECT * FROM closed_trades WHERE intent_id=? ORDER BY id", (intent_id,))
    orders = db.query(
        "SELECT client_order_id, role, side, order_type, status, executed_qty, avg_price, updated_at, payload_json FROM orders WHERE intent_id=?",
        (intent_id,),
    )
    entry_order = next((o for o in orders if o["role"] == "entry" and o["avg_price"]), None)
    planned_vs_actual = None
    exc = {"status": "not_applicable"}
    if entry_order:
        payload = json.loads(entry_order["payload_json"])
        planned_vs_actual = {
            "reference_price_at_send": payload.get("reference_price"),
            "actual_avg_entry": entry_order["avg_price"],
        }
        stop = Decimal(request["stop_price"]) if request.get("stop_price") else None
        closed_at = closed[-1]["closed_at"] if closed else None
        exc = excursions(
            market,
            intent["symbol"],
            Decimal(entry_order["avg_price"]),
            stop,
            entry_order["updated_at"],
            closed_at,
        )
    for order in orders:
        order.pop("payload_json", None)
    realized = sum((Decimal(c["realized_pnl"]) for c in closed), Decimal(0))
    events = db.journal_events(intent_id)
    return jsonable(
        {
            "intent_id": intent_id,
            "symbol": intent["symbol"],
            "mode": intent["mode"],
            "status": intent["status"],
            "request": request,
            "decision_report": json.loads(decision_rows[0]["payload_json"]) if decision_rows else None,
            "creation_validation": json.loads(intent["creation_report_json"])
            if intent["creation_report_json"]
            else None,
            "execution_validation": json.loads(intent["execution_report_json"])
            if intent["execution_report_json"]
            else None,
            "approvals": db.query(
                "SELECT approval_id, method, created_at, consumed_at FROM approvals WHERE intent_id=?",
                (intent_id,),
            ),
            "orders": orders,
            "planned_vs_actual": planned_vs_actual,
            "closed_trades": closed,
            "outcome": None
            if not closed
            else {
                "realized_pnl": realized,
                "result": "win" if realized > 0 else "loss" if realized < 0 else "breakeven",
            },
            "excursions": exc,
            "reviews": [e["payload"] for e in events if e["event_type"] == "post_trade_review"],
            "journal": [{"seq": e["seq"], "ts": e["ts"], "event": e["event_type"]} for e in events],
        }
    )


def submit_review(
    db: Database, knowledge: KnowledgeStore, review: PostTradeReview, author: str
) -> dict[str, Any]:
    if not db.query("SELECT 1 FROM intents WHERE intent_id=?", (review.intent_id,)):
        raise ValueError(f"unknown intent {review.intent_id}")
    if not db.query("SELECT 1 FROM closed_trades WHERE intent_id=?", (review.intent_id,)):
        raise ValueError("this trade has no closed position yet; review it after it closes")
    lesson_summary = None
    if review.lesson is not None:
        doc = knowledge.create_candidate_lesson(
            title=review.lesson.title,
            claim=review.lesson.claim,
            scope=review.lesson.scope,
            supporting_cases=[review.intent_id],
            contradicting_cases=review.lesson.contradicting_cases,
            author=author,
        )
        lesson_summary = doc.summary()
    entry = db.journal_append(
        review.intent_id,
        "post_trade_review",
        {"review": review, "lesson_id": lesson_summary["id"] if lesson_summary else None, "author": author},
    )
    return {
        "journal_seq": entry["seq"],
        "lesson": lesson_summary,
        "next_steps": [
            "The lesson is a CANDIDATE. It changes nothing in strategy, policy or skills.",
            "Turn it into a testable hypothesis and run it through the experiment registry (/strategy-research).",
            "Only the operator can promote it: python -m trader lesson promote <lesson_id> --experiment <experiment_id>",
        ]
        if lesson_summary
        else [],
    }

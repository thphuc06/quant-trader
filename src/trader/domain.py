"""Domain models shared by services, the MCP server and the CLI.

Money, prices and order quantities are ``Decimal``. Floats are used only for research statistics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class DecisionKind(StrEnum):
    NO_TRADE = "NO_TRADE"
    WATCH = "WATCH"
    LONG_CANDIDATE = "LONG_CANDIDATE"
    SHORT_CANDIDATE = "SHORT_CANDIDATE"
    EXIT_REVIEW = "EXIT_REVIEW"


class IntentStatus(StrEnum):
    REJECTED = "REJECTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTING = "EXECUTING"
    FILLED = "FILLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


OPEN_INTENT_STATUSES = (IntentStatus.PENDING_APPROVAL, IntentStatus.EXECUTING, IntentStatus.UNKNOWN)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymbolFilters(Strict):
    """Exchange trading rules for one symbol (Binance PRICE_FILTER / LOT_SIZE / NOTIONAL)."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal | None = None
    market_min_qty: Decimal | None = None
    market_max_qty: Decimal | None = None
    order_types: list[str] = Field(default_factory=list)  # empty = not reported by the source
    min_notional: Decimal
    max_notional: Decimal | None = None
    fetched_at: datetime


class Quote(Strict):
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    observed_at: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        if self.mid <= 0:
            return Decimal("Infinity")
        return (self.ask - self.bid) / self.mid * Decimal(10_000)


class Position(Strict):
    symbol: str
    quantity: Decimal
    avg_entry: Decimal
    stop_price: Decimal | None
    take_profit: Decimal | None
    opened_at: datetime
    intent_id: str
    last_checked_ms: int
    # PROTECTED: exchange stop confirmed. UNPROTECTED: no working stop. UNKNOWN: stop request outcome unknown.
    # SIMULATED: paper position, stop evaluated locally. NONE: no stop requested.
    protection: str = "UNKNOWN"
    stop_client_order_id: str | None = None


PROTECTED_STATES = ("PROTECTED", "SIMULATED", "NONE")


class PortfolioState(Strict):
    """Snapshot used by the risk engine. ``equity`` is quote balance plus positions at bid."""

    mode: str
    quote_asset: str
    free_quote: Decimal
    positions: list[Position]
    marks: dict[str, Decimal]
    equity: Decimal
    exposure: Decimal
    equity_peak: Decimal
    realized_pnl_today: Decimal
    realized_pnl_week: Decimal
    as_of: datetime


class CheckResult(Strict):
    name: str
    passed: bool
    detail: str


class SizingResult(Strict):
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    notional: Decimal
    risk_budget: Decimal
    loss_per_unit: Decimal
    worst_case_loss: Decimal
    binding_constraint: str
    notes: list[str] = Field(default_factory=list)


class ValidationReport(Strict):
    intent_id: str
    validated_at: datetime
    approved: bool
    checks: list[CheckResult]
    sizing: SizingResult | None
    policy_version: str
    policy_sha256: str
    mode: str
    stage: Literal["creation", "execution"]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


class TradeIntentRequest(Strict):
    """What the analyst may ask for. Note: no quantity field — sizing is computed server-side."""

    decision_id: str
    symbol: str
    side: Side
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    size_scale: Decimal = Decimal("1")
    rationale: str = Field(min_length=20, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=80)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("size_scale")
    @classmethod
    def _scale(cls, value: Decimal) -> Decimal:
        if not (Decimal("0") < value <= Decimal("1")):
            raise ValueError("size_scale must be in (0, 1]; risk can only be scaled down")
        return value

    @model_validator(mode="after")
    def _limit_needs_price(self) -> TradeIntentRequest:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT intent requires limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET intent must not set limit_price")
        return self


class TradeIntent(Strict):
    intent_id: str
    trace_id: str
    request: TradeIntentRequest
    status: IntentStatus
    quantity: Decimal | None
    created_at: datetime
    expires_at: datetime
    mode: str
    client_order_id: str


class Fill(Strict):
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    ts: datetime


class OrderResult(Strict):
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    status: Literal["FILLED", "PARTIALLY_FILLED", "NEW", "REJECTED", "CANCELED", "EXPIRED", "UNKNOWN"]
    requested_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal | None
    fills: list[Fill] = Field(default_factory=list)
    reference_price: Decimal | None = None
    detail: str = ""


# --------------------------------------------------------------------------- decision report


class RegimeRef(Strict):
    label: str = "unknown"
    model_or_method: str = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceItem(Strict):
    """One argument in a decision. ``kind`` separates facts from estimates and interpretation."""

    kind: Literal["observed_fact", "source_claim", "model_estimate", "interpretation", "hypothesis"]
    statement: str = Field(min_length=5, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    published_at: str | None = None
    observed_at: str | None = None


class Forecast(Strict):
    """A scoreable view (research/protocols/llm-decision-eval-v1.md): the probability that the close of the last closed
    1h bar ``horizon_hours`` after the report is recorded is above the close of the last closed 1h bar at recording.
    0.5 means no view. Reference and outcome prices are taken from stored candles by the scorer, never from here."""

    horizon_hours: int = Field(default=24, ge=1, le=168)
    probability_up: float = Field(ge=0.0, le=1.0)


class DecisionReport(Strict):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    as_of: datetime
    symbol: str
    instrument: Literal["spot"] = "spot"
    horizon: str
    decision: DecisionKind
    market_regime: RegimeRef = Field(default_factory=RegimeRef)
    quantitative_evidence: list[EvidenceItem] = Field(default_factory=list)
    ml_evidence: list[EvidenceItem] = Field(default_factory=list)
    news_evidence: list[EvidenceItem] = Field(default_factory=list)
    knowledge_references: list[str] = Field(default_factory=list)
    supporting_arguments: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    entry_plan: str | None = None
    stop_plan: str | None = None
    exit_plan: str | None = None
    expected_net_return: float | None = None
    risk_assessment: str | None = None
    source_freshness: dict[str, str] = Field(default_factory=dict)
    model_versions: list[str] = Field(default_factory=list)
    data_quality_status: Literal["ok", "degraded", "unknown"] = "unknown"
    llm_confidence_label: Literal["low", "medium", "high"] = "low"
    action_requested: Literal["NONE", "REQUEST_RISK_VALIDATION"] = "NONE"
    human_approval_required: bool = True
    reason_for_no_trade: str | None = None
    forecast: Forecast | None = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> DecisionReport:
        candidate = self.decision in (DecisionKind.LONG_CANDIDATE, DecisionKind.SHORT_CANDIDATE)
        if candidate:
            missing = [
                name
                for name, value in (
                    ("quantitative_evidence", self.quantitative_evidence),
                    ("counter_evidence", self.counter_evidence),
                    ("uncertainties", self.uncertainties),
                    ("invalidation_conditions", self.invalidation_conditions),
                    ("stop_plan", self.stop_plan),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"a trade candidate must fill: {', '.join(missing)}")
        if self.decision is DecisionKind.NO_TRADE and not self.reason_for_no_trade:
            raise ValueError("NO_TRADE requires reason_for_no_trade")
        if self.action_requested == "REQUEST_RISK_VALIDATION" and self.decision not in (
            DecisionKind.LONG_CANDIDATE,
            DecisionKind.EXIT_REVIEW,
        ):
            raise ValueError(
                "risk validation can only be requested for LONG_CANDIDATE or EXIT_REVIEW (spot, long/flat)"
            )
        if not self.human_approval_required:
            raise ValueError("human_approval_required cannot be false")
        return self

    def all_evidence_ids(self) -> set[str]:
        ids: set[str] = set(self.market_regime.evidence_ids)
        for group in (self.quantitative_evidence, self.ml_evidence, self.news_evidence):
            for item in group:
                ids.update(item.evidence_ids)
        return ids


class PredictionOutput(Strict):
    """ML prediction record. ``None`` means 'no value', never zero."""

    symbol: str
    timeframe: str
    horizon: str
    prediction_timestamp: str
    feature_cutoff_timestamp: str
    model_name: str
    model_version: str
    p_up: float | None = None
    p_down: float | None = None
    p_neutral: float | None = None
    expected_net_return: float | None = None
    expected_return_interval: list[float] | None = None
    calibration_status: Literal["not_evaluated", "evaluated_ok", "evaluated_poor"] = "not_evaluated"
    out_of_sample_status: Literal["not_evaluated", "evaluated_no_edge", "evaluated_passed_gates"] = (
        "not_evaluated"
    )
    data_quality: Literal["ok", "degraded", "unknown"] = "unknown"
    usable_for_decisions: bool = False
    notes: list[str] = Field(default_factory=list)


def jsonable(value: Any) -> Any:
    """Convert models/Decimals/datetimes to JSON-safe structures (Decimal -> str)."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(v) for v in value]
    return value

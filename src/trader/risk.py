"""Risk engine: deterministic position sizing and policy validation.

The engine never trusts numbers from the analyst. It receives a request (no quantity) plus a context of
facts gathered by the backend, and returns an immutable report. Any missing fact or raised exception
turns into a failed check: fail closed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trader.config import LoadedPolicy, TradingMode
from trader.domain import (
    PROTECTED_STATES,
    CheckResult,
    OrderType,
    PortfolioState,
    Quote,
    Side,
    SizingResult,
    SymbolFilters,
    TradeIntentRequest,
    ValidationReport,
)
from trader.exchanges.binance.filters import check_order, round_quantity

HUNDRED = Decimal(100)
BPS = Decimal(10_000)


@dataclass
class RiskContext:
    mode: TradingMode
    loaded: LoadedPolicy
    now: datetime
    intent_expires_at: datetime
    quote: Quote | None
    quote_volume_24h: Decimal | None
    filters: SymbolFilters | None
    portfolio: PortfolioState | None
    kill_switch_engaged: bool
    mode_permitted: tuple[bool, str]
    other_open_intents_same_symbol: int
    unknown_intents_same_symbol: int
    last_loss_close: datetime | None
    candle_staleness_bars: float | None
    fee_rate: Decimal
    slippage_rate: Decimal
    reconciliation: tuple[bool, str] = (True, "not applicable")
    approved_quantity_cap: Decimal | None = None


def compute_sizing(
    *,
    equity: Decimal,
    free_quote: Decimal,
    current_exposure: Decimal,
    entry: Decimal,
    stop: Decimal,
    size_scale: Decimal,
    max_risk_pct: Decimal,
    max_position_notional_pct: Decimal,
    max_total_exposure_pct: Decimal,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    filters: SymbolFilters,
    order_type: OrderType,
) -> SizingResult:
    """Largest quantity that respects the risk budget, notional cap, exposure cap and free balance.

    loss_per_unit = (entry - stop) + cost of entering + cost of exiting at the stop.
    Quantity is rounded DOWN to the exchange step.
    """
    if entry <= 0 or stop <= 0 or stop >= entry:
        raise ValueError("stop must be positive and below the entry price for a long position")
    if equity <= 0:
        raise ValueError("equity must be positive")
    cost_rate = fee_rate + slippage_rate
    risk_budget = equity * max_risk_pct / HUNDRED * size_scale
    loss_per_unit = (entry - stop) + entry * cost_rate + stop * cost_rate
    candidates = {
        "risk_budget": risk_budget / loss_per_unit,
        "max_position_notional": equity * max_position_notional_pct / HUNDRED / entry,
        "max_total_exposure": max(Decimal(0), equity * max_total_exposure_pct / HUNDRED - current_exposure)
        / entry,
        "free_balance": free_quote / (entry * (1 + cost_rate)),
    }
    binding = min(candidates, key=lambda k: candidates[k])
    quantity = round_quantity(candidates[binding], filters, order_type)
    notes = [f"{name}: max {value:.8f}" for name, value in candidates.items()]
    return SizingResult(
        quantity=quantity,
        entry_price=entry,
        stop_price=stop,
        notional=quantity * entry,
        risk_budget=risk_budget,
        loss_per_unit=loss_per_unit,
        worst_case_loss=quantity * loss_per_unit,
        binding_constraint=binding,
        notes=notes,
    )


class _Checks:
    def __init__(self) -> None:
        self.items: list[CheckResult] = []

    def add(self, name: str, passed: bool, detail: str) -> bool:
        self.items.append(CheckResult(name=name, passed=bool(passed), detail=detail))
        return bool(passed)

    def guard(self, name: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        """Run a check; an exception is a failure, never a pass."""
        try:
            passed, detail = fn()
        except Exception as exc:
            return self.add(name, False, f"check raised {type(exc).__name__}: {exc}")
        return self.add(name, passed, detail)


def validate_intent(
    intent_id: str, request: TradeIntentRequest, ctx: RiskContext, stage: str
) -> ValidationReport:
    policy = ctx.loaded.policy
    checks = _Checks()
    sizing: SizingResult | None = None
    opening = request.side is Side.BUY

    checks.add("mode_permitted", ctx.mode_permitted[0], ctx.mode_permitted[1])
    checks.add(
        "symbol_allowed",
        request.symbol in policy.allowed_symbols,
        f"{request.symbol}; allowed: {policy.allowed_symbols}",
    )
    checks.add(
        "order_type_allowed", request.order_type.value in policy.allowed_order_types, request.order_type.value
    )
    checks.add(
        "intent_not_expired", ctx.now < ctx.intent_expires_at, f"expires {ctx.intent_expires_at.isoformat()}"
    )
    checks.add(
        "no_unresolved_order_state",
        ctx.unknown_intents_same_symbol == 0,
        f"{ctx.unknown_intents_same_symbol} intent(s) in UNKNOWN state for this symbol; reconcile first",
    )
    checks.add(
        "no_duplicate_open_intent",
        ctx.other_open_intents_same_symbol == 0,
        f"{ctx.other_open_intents_same_symbol} other open intent(s) for this symbol",
    )
    checks.add("exchange_state_reconciled", ctx.reconciliation[0], ctx.reconciliation[1])

    quote, filters, portfolio = ctx.quote, ctx.filters, ctx.portfolio
    have_quote = checks.add(
        "quote_available", quote is not None, "best bid/ask present" if quote else "no quote"
    )
    have_filters = checks.add(
        "exchange_filters_available",
        filters is not None,
        "filters present" if filters else "no exchange filters",
    )
    have_portfolio = checks.add(
        "portfolio_available",
        portfolio is not None,
        "portfolio state present" if portfolio else "portfolio unavailable",
    )

    if have_quote and quote is not None:
        age = (ctx.now - quote.observed_at).total_seconds()
        checks.add(
            "quote_fresh",
            0 <= age <= policy.max_quote_age_seconds,
            f"quote age {age:.1f}s (limit {policy.max_quote_age_seconds}s)",
        )
        checks.add("quote_sane", quote.bid > 0 and quote.ask >= quote.bid, f"bid {quote.bid} ask {quote.ask}")
        checks.add(
            "spread_within_limit",
            quote.spread_bps <= policy.max_spread_bps,
            f"spread {quote.spread_bps:.2f} bps (limit {policy.max_spread_bps})",
        )

    if opening:
        checks.add(
            "side_allowed_to_open", "BUY" in policy.allowed_open_sides, "BUY opens a long spot position"
        )
        checks.add(
            "kill_switch_clear",
            not ctx.kill_switch_engaged,
            "kill switch engaged: no new positions" if ctx.kill_switch_engaged else "clear",
        )
        checks.add(
            "liquidity_sufficient",
            ctx.quote_volume_24h is not None and ctx.quote_volume_24h >= policy.min_24h_quote_volume,
            f"24h quote volume {ctx.quote_volume_24h} (min {policy.min_24h_quote_volume})",
        )
        checks.add(
            "candles_fresh",
            ctx.candle_staleness_bars is not None
            and ctx.candle_staleness_bars <= policy.max_candle_staleness_bars,
            f"last closed bar is {ctx.candle_staleness_bars} bars old (limit {policy.max_candle_staleness_bars})",
        )
        expected_slip_bps = ctx.slippage_rate * BPS + (quote.spread_bps / 2 if quote else Decimal(0))
        checks.add(
            "expected_slippage_within_limit",
            expected_slip_bps <= policy.max_expected_slippage_bps,
            f"{expected_slip_bps:.2f} bps (limit {policy.max_expected_slippage_bps})",
        )
        if ctx.last_loss_close is not None:
            until = ctx.last_loss_close + timedelta(minutes=policy.cooldown_after_loss_minutes)
            checks.add("cooldown_after_loss", ctx.now >= until, f"cooldown until {until.isoformat()}")

        entry: Decimal | None = None
        if have_quote and quote is not None:
            if request.order_type is OrderType.MARKET:
                entry = quote.ask
            else:
                assert request.limit_price is not None
                marketable = request.limit_price >= quote.ask
                checks.add(
                    "limit_is_marketable",
                    marketable,
                    f"limit {request.limit_price} vs ask {quote.ask}; resting limit orders are not supported",
                )
                entry = quote.ask if marketable else None

        stop = request.stop_price
        stop_ok = False
        if policy.require_stop_loss and stop is None:
            checks.add("stop_loss_present", False, "policy requires a stop_price")
        elif stop is not None and entry is not None:
            distance_pct = (entry - stop) / entry * HUNDRED
            stop_ok = checks.add(
                "stop_distance_within_limits",
                policy.min_stop_distance_pct <= distance_pct <= policy.max_stop_distance_pct,
                f"stop is {distance_pct:.3f}% below entry (allowed {policy.min_stop_distance_pct}%..{policy.max_stop_distance_pct}%)",
            )
        if request.take_profit is not None and entry is not None:
            checks.add(
                "take_profit_above_entry",
                request.take_profit > entry,
                f"target {request.take_profit} vs entry {entry}",
            )
            if policy.min_reward_risk is not None and stop is not None and entry > stop:
                rr = (request.take_profit - entry) / (entry - stop)
                checks.add(
                    "reward_risk_minimum",
                    rr >= policy.min_reward_risk,
                    f"reward/risk {rr:.2f} (min {policy.min_reward_risk})",
                )

        if have_portfolio and portfolio is not None:
            held = {p.symbol for p in portfolio.positions if p.quantity > 0}
            checks.add(
                "no_existing_position",
                request.symbol not in held,
                "one position per symbol; adding to a position is not supported",
            )
            exposed = sorted(f"{p.symbol}:{p.protection}" for p in portfolio.positions if p.protection not in PROTECTED_STATES)
            checks.add(
                "all_positions_protected",
                not exposed,
                f"positions without a confirmed stop: {exposed}; run sync_positions / protect_position first"
                if exposed
                else "every open position has a confirmed or simulated stop",
            )
            checks.add(
                "max_open_positions",
                len(held) < policy.max_open_positions,
                f"{len(held)} open (limit {policy.max_open_positions})",
            )
            equity = portfolio.equity
            checks.add("equity_positive", equity > 0, f"equity {equity}")
            if equity > 0:
                daily_limit = -equity * policy.daily_loss_limit_pct / HUNDRED
                weekly_limit = -equity * policy.weekly_loss_limit_pct / HUNDRED
                checks.add(
                    "daily_loss_limit",
                    portfolio.realized_pnl_today > daily_limit,
                    f"realized today {portfolio.realized_pnl_today:.2f} (limit {daily_limit:.2f})",
                )
                checks.add(
                    "weekly_loss_limit",
                    portfolio.realized_pnl_week > weekly_limit,
                    f"realized 7d {portfolio.realized_pnl_week:.2f} (limit {weekly_limit:.2f})",
                )
                floor = portfolio.equity_peak * (1 - policy.max_drawdown_kill_pct / HUNDRED)
                checks.add(
                    "drawdown_within_limit",
                    equity > floor,
                    f"equity {equity:.2f} vs kill level {floor:.2f} (peak {portfolio.equity_peak:.2f})",
                )

            if (
                stop_ok
                and entry is not None
                and stop is not None
                and have_filters
                and filters is not None
                and equity > 0
            ):

                def size() -> tuple[bool, str]:
                    nonlocal sizing
                    result = compute_sizing(
                        equity=equity,
                        free_quote=portfolio.free_quote,
                        current_exposure=portfolio.exposure,
                        entry=entry,
                        stop=stop,
                        size_scale=request.size_scale,
                        max_risk_pct=policy.max_risk_per_trade_pct,
                        max_position_notional_pct=policy.max_position_notional_pct,
                        max_total_exposure_pct=policy.max_total_exposure_pct,
                        fee_rate=ctx.fee_rate,
                        slippage_rate=ctx.slippage_rate,
                        filters=filters,
                        order_type=request.order_type,
                    )
                    if ctx.approved_quantity_cap is not None and result.quantity > ctx.approved_quantity_cap:
                        capped = ctx.approved_quantity_cap
                        result = result.model_copy(
                            update={
                                "quantity": capped,
                                "notional": capped * entry,
                                "worst_case_loss": capped * result.loss_per_unit,
                                "binding_constraint": "approved_quantity",
                            }
                        )
                    sizing = result
                    return (
                        result.quantity > 0,
                        f"quantity {result.quantity} (binding: {result.binding_constraint}); worst-case loss {result.worst_case_loss:.4f}",
                    )

                if checks.guard("position_sizing", size) and sizing is not None:
                    problems = check_order(sizing.quantity, entry, filters, request.order_type)
                    checks.add("exchange_filters_satisfied", not problems, "; ".join(problems) or "ok")
    else:
        # SELL: reduce-only. Allowed while the kill switch is engaged; never opens a short.
        position = next(
            (
                p
                for p in (portfolio.positions if portfolio else [])
                if p.symbol == request.symbol and p.quantity > 0
            ),
            None,
        )
        if (
            checks.add(
                "position_exists_to_reduce",
                position is not None,
                "spot long/flat: SELL may only reduce an existing position",
            )
            and position
            and quote
            and filters
        ):
            quantity = round_quantity(position.quantity * request.size_scale, filters, request.order_type)
            if request.order_type is OrderType.LIMIT:
                assert request.limit_price is not None
                checks.add(
                    "limit_is_marketable",
                    request.limit_price <= quote.bid,
                    f"limit {request.limit_price} vs bid {quote.bid}",
                )
            if ctx.approved_quantity_cap is not None:
                quantity = min(quantity, ctx.approved_quantity_cap)
            checks.add(
                "reduce_quantity_positive",
                quantity > 0,
                f"quantity {quantity} of position {position.quantity}",
            )
            problems = (
                check_order(quantity, quote.bid, filters, request.order_type)
                if quantity > 0
                else ["quantity is zero"]
            )
            checks.add("exchange_filters_satisfied", not problems, "; ".join(problems) or "ok")
            sizing = SizingResult(
                quantity=quantity,
                entry_price=quote.bid,
                stop_price=quote.bid,
                notional=quantity * quote.bid,
                risk_budget=Decimal(0),
                loss_per_unit=Decimal(0),
                worst_case_loss=Decimal(0),
                binding_constraint="reduce_only",
                notes=["reduce-only sell; entry_price is the current bid"],
            )

    approved = all(c.passed for c in checks.items) and sizing is not None and sizing.quantity > 0
    return ValidationReport(
        intent_id=intent_id,
        validated_at=ctx.now,
        approved=approved,
        checks=checks.items,
        sizing=sizing,
        policy_version=policy.version,
        policy_sha256=ctx.loaded.sha256,
        mode=ctx.mode.value,
        stage=stage,  # type: ignore[arg-type]
    )

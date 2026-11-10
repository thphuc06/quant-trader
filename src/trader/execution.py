"""Execution boundary: intent ledger, approvals, brokers, reconciliation, kill switch, live lock.

Nothing in this module trusts its caller. Quantities come from the risk engine, every execution is
re-validated with fresh data, and an order whose outcome is unknown is reconciled, never resent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from trader.config import AppConfig, LoadedPolicy, Settings, TradingMode, load_risk_policy
from trader.domain import (
    OPEN_INTENT_STATUSES,
    Fill,
    IntentStatus,
    OrderResult,
    OrderType,
    PortfolioState,
    Position,
    Quote,
    Side,
    SymbolFilters,
    TradeIntent,
    TradeIntentRequest,
    ValidationReport,
    jsonable,
    new_id,
)
from trader.exchanges.binance.client import BinanceAPIError, BinanceTradingClient, OrderStateUnknown
from trader.exchanges.binance.filters import round_price, round_quantity
from trader.marketdata import MarketData
from trader.risk import RiskContext, validate_intent
from trader.storage.candles import check_quality, drop_unclosed, klines_to_frame
from trader.storage.db import Database, canonical_json
from trader.timeutil import from_ms, iso, parse_iso, to_ms, utc_now

BPS = Decimal(10_000)
KILL_SWITCH_KEY = "kill_switch"
STOP_LIMIT_BUFFER = Decimal("0.005")  # stop-limit sell price sits 0.5% under the trigger


class ExecutionError(ValueError):
    """The request cannot be carried out as stated (reported to the caller as 'rejected')."""


# --------------------------------------------------------------------------- brokers


class Broker(Protocol):
    def free_quote(self, quote_asset: str) -> Decimal: ...

    def asset_total(self, asset: str) -> Decimal | None: ...

    def execute(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        limit_price: Decimal | None,
        client_order_id: str,
        quote: Quote,
    ) -> OrderResult: ...

    def find_order(self, symbol: str, client_order_id: str) -> OrderResult | None: ...

    def place_protective_stop(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        filters: SymbolFilters,
        client_order_id: str,
    ) -> OrderResult | None: ...

    def cancel(self, symbol: str, client_order_id: str) -> None: ...


class PaperBroker:
    """Local simulator. Fills at the real best ask/bid plus configured slippage; fee charged in quote."""

    def __init__(
        self,
        db: Database,
        starting_quote: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        quote_asset: str = "USDT",
    ) -> None:
        self.db = db
        self.fee_rate = fee_bps / BPS
        self.slippage_rate = slippage_bps / BPS
        if not db.query("SELECT 1 FROM paper_balances WHERE asset=?", (quote_asset,)):
            db.execute(
                "INSERT INTO paper_balances(asset, free) VALUES (?, ?)",
                (quote_asset, format(starting_quote, "f")),
            )

    def free_quote(self, quote_asset: str) -> Decimal:
        rows = self.db.query("SELECT free FROM paper_balances WHERE asset=?", (quote_asset,))
        return Decimal(rows[0]["free"]) if rows else Decimal(0)

    def asset_total(self, asset: str) -> Decimal | None:
        return None  # paper positions live in the positions table only

    def execute(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        limit_price: Decimal | None,
        client_order_id: str,
        quote: Quote,
    ) -> OrderResult:
        existing = self.find_order(symbol, client_order_id)
        if existing is not None:
            return existing  # idempotent replay
        if side is Side.BUY:
            price = quote.ask * (1 + self.slippage_rate)
            if limit_price is not None:
                if quote.ask > limit_price:
                    return self._unfilled(
                        symbol,
                        side,
                        order_type,
                        quantity,
                        client_order_id,
                        quote.ask,
                        "limit below ask (IOC, not filled)",
                    )
                price = min(price, limit_price)
        else:
            price = quote.bid * (1 - self.slippage_rate)
            if limit_price is not None:
                if quote.bid < limit_price:
                    return self._unfilled(
                        symbol,
                        side,
                        order_type,
                        quantity,
                        client_order_id,
                        quote.bid,
                        "limit above bid (IOC, not filled)",
                    )
                price = max(price, limit_price)
        notional = quantity * price
        fee = notional * self.fee_rate
        quote_asset = "USDT"
        free = self.free_quote(quote_asset)
        delta = -(notional + fee) if side is Side.BUY else (notional - fee)
        if free + delta < 0:
            return OrderResult(
                client_order_id=client_order_id, exchange_order_id=None, symbol=symbol, side=side, order_type=order_type, status="REJECTED",
                requested_qty=quantity, executed_qty=Decimal(0), avg_price=None, detail="insufficient paper balance",
            )  # fmt: skip
        self.db.execute(
            "UPDATE paper_balances SET free=? WHERE asset=?", (format(free + delta, "f"), quote_asset)
        )
        return OrderResult(
            client_order_id=client_order_id,
            exchange_order_id=f"paper-{client_order_id}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            status="FILLED",
            requested_qty=quantity,
            executed_qty=quantity,
            avg_price=price,
            fills=[Fill(price=price, quantity=quantity, fee=fee, fee_asset=quote_asset, ts=utc_now())],
            reference_price=quote.ask if side is Side.BUY else quote.bid,
            detail="paper fill at best quote plus configured slippage; no partial fills simulated",
        )

    @staticmethod
    def _unfilled(
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        client_order_id: str,
        ref: Decimal,
        why: str,
    ) -> OrderResult:
        return OrderResult(
            client_order_id=client_order_id, exchange_order_id=None, symbol=symbol, side=side, order_type=order_type, status="EXPIRED",
            requested_qty=quantity, executed_qty=Decimal(0), avg_price=None, reference_price=ref, detail=why,
        )  # fmt: skip

    def find_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        rows = self.db.query("SELECT payload_json FROM orders WHERE client_order_id=?", (client_order_id,))
        return OrderResult.model_validate_json(rows[0]["payload_json"]) if rows else None

    def place_protective_stop(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        filters: SymbolFilters,
        client_order_id: str,
    ) -> OrderResult | None:
        return None  # paper stops are evaluated against historical bars by monitor_positions()

    def cancel(self, symbol: str, client_order_id: str) -> None:
        return None


class BinanceBroker:
    """Demo/Live adapter. Verified against mocked HTTP only (see architecture/adr/0005)."""

    def __init__(self, client: BinanceTradingClient) -> None:
        self.client = client

    def _balances(self) -> dict[str, Decimal]:
        account = self.client.account()
        return {
            b["asset"]: Decimal(b["free"]) + Decimal(b.get("locked", "0"))
            for b in account.get("balances", [])
        }

    def free_quote(self, quote_asset: str) -> Decimal:
        account = self.client.account()
        for balance in account.get("balances", []):
            if balance["asset"] == quote_asset:
                return Decimal(balance["free"])
        return Decimal(0)

    def asset_total(self, asset: str) -> Decimal | None:
        return self._balances().get(asset, Decimal(0))

    def execute(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        limit_price: Decimal | None,
        client_order_id: str,
        quote: Quote,
    ) -> OrderResult:
        result = self.client.new_order(
            symbol=symbol,
            side=side,
            order_type=order_type.value,
            quantity=quantity,
            client_order_id=client_order_id,
            price=limit_price if order_type is OrderType.LIMIT else None,
            time_in_force="IOC" if order_type is OrderType.LIMIT else None,
        )
        return result.model_copy(update={"reference_price": quote.ask if side is Side.BUY else quote.bid})

    def find_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        return self.client.get_order(symbol, client_order_id)

    def place_protective_stop(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        filters: SymbolFilters,
        client_order_id: str,
    ) -> OrderResult | None:
        trigger = round_price(stop_price, filters)
        limit = round_price(stop_price * (1 - STOP_LIMIT_BUFFER), filters)
        return self.client.new_order(
            symbol=symbol, side=Side.SELL, order_type="STOP_LOSS_LIMIT", quantity=quantity, client_order_id=client_order_id,
            price=limit, stop_price=trigger, time_in_force="GTC",
        )  # fmt: skip

    def cancel(self, symbol: str, client_order_id: str) -> None:
        self.client.cancel_order(symbol, client_order_id)


# --------------------------------------------------------------------------- service


def approval_params_hash(intent: TradeIntent) -> str:
    r = intent.request
    material = {
        "intent_id": intent.intent_id, "symbol": r.symbol, "side": r.side.value, "order_type": r.order_type.value,
        "limit_price": r.limit_price, "stop_price": r.stop_price, "take_profit": r.take_profit, "quantity": intent.quantity, "mode": intent.mode,
    }  # fmt: skip
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


class ExecutionService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        app: AppConfig,
        root: Path,
        market: MarketData,
        broker: Broker | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.app = app
        self.root = root
        self.market = market
        self.mode = settings.trading_mode
        self._broker = broker
        self._broker_error: str | None = None

    # ------------------------------------------------------------------ policy / mode / kill switch

    def policy(self) -> LoadedPolicy:
        return load_risk_policy(self.root)

    def live_gate(self, policy: LoadedPolicy | None = None) -> tuple[bool, list[str]]:
        """All conditions that must hold before LIVE orders are possible."""
        reasons = []
        if self.settings.trading_mode is not TradingMode.LIVE:
            reasons.append("TRADING_MODE is not LIVE")
        if not self.settings.live_trading_enabled:
            reasons.append("LIVE_TRADING_ENABLED is false")
        if self.settings.credentials_for(TradingMode.LIVE) is None:
            reasons.append("live API credentials are not configured")
        try:
            loaded = policy or self.policy()
            if loaded.policy.status != "approved":
                reasons.append("risk policy status is not 'approved'")
        except Exception as exc:
            reasons.append(f"risk policy cannot be loaded: {type(exc).__name__}")
        unlock = self.settings.data_dir(self.root) / "live_unlock.json"
        try:
            expires = parse_iso(json.loads(unlock.read_text(encoding="utf-8"))["expires_at"])
            if utc_now() >= expires:
                reasons.append("live unlock has expired; run `python -m trader live-unlock` at a terminal")
        except (OSError, ValueError, KeyError):
            reasons.append("no live unlock; run `python -m trader live-unlock` at a terminal")
        return (not reasons), reasons

    def mode_permitted(self, policy: LoadedPolicy | None = None) -> tuple[bool, str]:
        if self.mode is TradingMode.PAPER:
            return True, "PAPER: simulated execution only"
        if self.mode is TradingMode.BINANCE_DEMO:
            if self.settings.credentials_for(TradingMode.BINANCE_DEMO) is None:
                return False, "BINANCE_DEMO credentials are not configured"
            return True, "BINANCE_DEMO: Binance Demo Mode, no real funds"
        ok, reasons = self.live_gate(policy)
        return (True, "LIVE unlocked") if ok else (False, "live_disabled: " + "; ".join(reasons))

    def broker(self) -> Broker:
        if self._broker is not None:
            return self._broker
        if self.mode is TradingMode.PAPER:
            paper = self.app.paper
            self._broker = PaperBroker(
                self.db,
                paper.starting_balance_usdt,
                paper.fee_bps,
                paper.slippage_bps,
                self.app.universe["quote_asset"],
            )
            return self._broker
        ok, why = self.mode_permitted()
        if not ok:
            raise ExecutionError(why)
        credentials = self.settings.credentials_for(self.mode)
        assert credentials is not None
        client = BinanceTradingClient(
            self.app.endpoints.trading_rest(self.mode), credentials[0], credentials[1], self.app.http
        )
        self._broker = BinanceBroker(client)
        return self._broker

    def kill_switch(self) -> dict[str, Any]:
        return self.db.kv_get(
            KILL_SWITCH_KEY, {"engaged": False, "reason": None, "engaged_at": None, "by": None}
        )

    def engage_kill_switch(self, reason: str, by: str, cancel_open_orders: bool = False) -> dict[str, Any]:
        state = {"engaged": True, "reason": reason[:300], "engaged_at": iso(utc_now()), "by": by}
        self.db.kv_set(KILL_SWITCH_KEY, state)
        cancelled = []
        if cancel_open_orders:
            for row in self.db.query(
                "SELECT intent_id FROM intents WHERE status=? AND side='BUY'",
                (IntentStatus.PENDING_APPROVAL.value,),
            ):
                self._set_status(
                    row["intent_id"], IntentStatus.CANCELLED, expected=IntentStatus.PENDING_APPROVAL
                )
                cancelled.append(row["intent_id"])
        self.db.journal_append("system", "kill_switch_engaged", {**state, "cancelled_intents": cancelled})
        return {
            **state,
            "cancelled_pending_entry_intents": cancelled,
            "note": "New positions are blocked. Protective stops are untouched. Positions are NOT closed automatically; "
            "release requires `python -m trader kill-switch release` at a terminal.",
        }

    def release_kill_switch(self, by: str) -> dict[str, Any]:
        state = {
            "engaged": False,
            "reason": None,
            "engaged_at": None,
            "by": by,
            "released_at": iso(utc_now()),
        }
        self.db.kv_set(KILL_SWITCH_KEY, state)
        self.db.journal_append("system", "kill_switch_released", state)
        return state

    # ------------------------------------------------------------------ portfolio

    def fee_rate(self) -> Decimal:
        if self.mode is TradingMode.PAPER:
            return self.app.paper.fee_bps / BPS
        return Decimal(str(self.app.costs.fee_bps)) / BPS

    def slippage_rate(self) -> Decimal:
        if self.mode is TradingMode.PAPER:
            return self.app.paper.slippage_bps / BPS
        return Decimal(str(self.app.costs.slippage_bps)) / BPS

    def positions(self) -> list[Position]:
        rows = self.db.query("SELECT * FROM positions WHERE mode=? ORDER BY symbol", (self.mode.value,))
        return [
            Position(
                symbol=r["symbol"],
                quantity=Decimal(r["quantity"]),
                avg_entry=Decimal(r["avg_entry"]),
                stop_price=Decimal(r["stop_price"]) if r["stop_price"] else None,
                take_profit=Decimal(r["take_profit"]) if r["take_profit"] else None,
                opened_at=parse_iso(r["opened_at"]),
                intent_id=r["intent_id"],
                last_checked_ms=int(r["last_checked_ms"]),
            )
            for r in rows
        ]

    def portfolio(self, known_quotes: dict[str, Quote] | None = None) -> PortfolioState:
        """Equity = free quote balance + positions marked at the bid. Raises if any input is unavailable."""
        quote_asset = self.app.universe["quote_asset"]
        free = self.broker().free_quote(quote_asset)
        positions = self.positions()
        marks: dict[str, Decimal] = {}
        for position in positions:
            quote = (known_quotes or {}).get(position.symbol) or self.market.source.quote(position.symbol)
            marks[position.symbol] = quote.bid
        exposure = sum((p.quantity * marks[p.symbol] for p in positions), Decimal(0))
        equity = free + exposure
        peak_key = f"equity_peak:{self.mode.value}"
        peak = max(Decimal(self.db.kv_get(peak_key, "0")), equity)
        self.db.kv_set(peak_key, format(peak, "f"))
        now = utc_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return PortfolioState(
            mode=self.mode.value,
            quote_asset=quote_asset,
            free_quote=free,
            positions=positions,
            marks=marks,
            equity=equity,
            exposure=exposure,
            equity_peak=peak,
            realized_pnl_today=self._realized_since(day_start),
            realized_pnl_week=self._realized_since(now - timedelta(days=7)),
            as_of=now,
        )

    def _realized_since(self, since: datetime) -> Decimal:
        rows = self.db.query(
            "SELECT realized_pnl FROM closed_trades WHERE mode=? AND closed_at>=?",
            (self.mode.value, iso(since)),
        )
        return sum((Decimal(r["realized_pnl"]) for r in rows), Decimal(0))

    def _reconciliation(self, filters_by_symbol: dict[str, SymbolFilters]) -> tuple[bool, str]:
        if self.mode is TradingMode.PAPER:
            return True, "paper ledger is authoritative"
        problems = []
        for position in self.positions():
            base = (filters_by_symbol.get(position.symbol) or self.market.filters(position.symbol)).base_asset
            held = self.broker().asset_total(base)
            if held is None or held < position.quantity * Decimal("0.999"):
                problems.append(f"{position.symbol}: local {position.quantity} vs exchange {held}")
        return (not problems), ("; ".join(problems) or "local positions match exchange balances")

    # ------------------------------------------------------------------ intents

    def _row_to_intent(self, row: dict[str, Any]) -> TradeIntent:
        return TradeIntent(
            intent_id=row["intent_id"],
            trace_id=row["trace_id"],
            request=TradeIntentRequest.model_validate_json(row["request_json"]),
            status=IntentStatus(row["status"]),
            quantity=Decimal(row["quantity"]) if row["quantity"] else None,
            created_at=parse_iso(row["created_at"]),
            expires_at=parse_iso(row["expires_at"]),
            mode=row["mode"],
            client_order_id=row["client_order_id"],
        )

    def get_intent(self, intent_id: str) -> TradeIntent | None:
        rows = self.db.query("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
        return self._row_to_intent(rows[0]) if rows else None

    def intent_details(self, intent_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
        if not rows:
            return None
        row = rows[0]
        intent = self._row_to_intent(row)
        if intent.status is IntentStatus.UNKNOWN:
            self.reconcile(intent_id)
            row = self.db.query("SELECT * FROM intents WHERE intent_id=?", (intent_id,))[0]
            intent = self._row_to_intent(row)
        elif intent.status is IntentStatus.PENDING_APPROVAL and utc_now() >= intent.expires_at:
            self._set_status(intent_id, IntentStatus.EXPIRED, expected=IntentStatus.PENDING_APPROVAL)
            intent = intent.model_copy(update={"status": IntentStatus.EXPIRED})
        return {
            "intent": jsonable(intent),
            "creation_report": json.loads(row["creation_report_json"])
            if row["creation_report_json"]
            else None,
            "execution_report": json.loads(row["execution_report_json"])
            if row["execution_report_json"]
            else None,
            "orders": self.db.query(
                "SELECT client_order_id, role, side, order_type, status, requested_qty, executed_qty, avg_price, updated_at FROM orders WHERE intent_id=?",
                (intent_id,),
            ),
        }

    def open_intents(self) -> list[dict[str, Any]]:
        statuses = tuple(s.value for s in OPEN_INTENT_STATUSES)
        return self.db.query(
            f"SELECT intent_id, symbol, side, status, quantity, created_at, expires_at FROM intents WHERE mode=? AND status IN ({','.join('?' * len(statuses))})",
            (self.mode.value, *statuses),
        )

    def _set_status(
        self, intent_id: str, status: IntentStatus, *, expected: IntentStatus | None = None, **columns: Any
    ) -> bool:
        sets, params = ["status=?"], [status.value]
        for name, value in columns.items():
            sets.append(f"{name}=?")
            params.append(value)
        sql = f"UPDATE intents SET {', '.join(sets)} WHERE intent_id=?"
        params.append(intent_id)
        if expected is not None:
            sql += " AND status=?"
            params.append(expected.value)
        changed = self.db.execute(sql, tuple(params)) == 1
        if changed:
            self.db.journal_append(intent_id, "intent_status", {"status": status.value})
        return changed

    def _context(
        self, request: TradeIntentRequest, intent_id: str, expires_at: datetime, cap: Decimal | None
    ) -> RiskContext:
        """Gather facts for the risk engine. Anything unavailable is passed as None so the check fails."""
        loaded = self.policy()  # raising here aborts the whole operation: fail closed
        quote = filters = portfolio = volume = staleness = None
        reconciliation: tuple[bool, str] = (False, "not evaluated")
        try:
            quote = self.market.source.quote(request.symbol)
        except Exception:
            quote = None
        try:
            filters = self.market.filters(request.symbol, max_age_seconds=300)
        except Exception:
            filters = None
        try:
            volume = Decimal(str(self.market.source.ticker_24h(request.symbol)["quoteVolume"]))
        except Exception:
            volume = None
        try:
            interval = self.app.decision_timeframe
            frame = self.market.refresh(request.symbol, interval, min_bars=50)
            staleness = check_quality(frame, request.symbol, interval, to_ms(utc_now())).staleness_bars
        except Exception:
            staleness = None
        try:
            portfolio = self.portfolio({request.symbol: quote} if quote else None)
            reconciliation = self._reconciliation({request.symbol: filters} if filters else {})
        except Exception as exc:
            portfolio, reconciliation = None, (False, f"portfolio unavailable: {type(exc).__name__}")
        statuses = tuple(s.value for s in OPEN_INTENT_STATUSES)
        open_rows = self.db.query(
            f"SELECT intent_id, status FROM intents WHERE mode=? AND symbol=? AND intent_id<>? AND status IN ({','.join('?' * len(statuses))})",
            (self.mode.value, request.symbol, intent_id, *statuses),
        )
        loss_rows = self.db.query(
            "SELECT closed_at FROM closed_trades WHERE mode=? AND symbol=? AND CAST(realized_pnl AS REAL) < 0 ORDER BY closed_at DESC LIMIT 1",
            (self.mode.value, request.symbol),
        )
        return RiskContext(
            mode=self.mode,
            loaded=loaded,
            now=utc_now(),
            intent_expires_at=expires_at,
            quote=quote,
            quote_volume_24h=volume,
            filters=filters,
            portfolio=portfolio,
            kill_switch_engaged=bool(self.kill_switch().get("engaged")),
            mode_permitted=self.mode_permitted(loaded),
            other_open_intents_same_symbol=sum(
                1 for r in open_rows if r["status"] != IntentStatus.UNKNOWN.value
            ),
            unknown_intents_same_symbol=sum(
                1 for r in open_rows if r["status"] == IntentStatus.UNKNOWN.value
            ),
            last_loss_close=parse_iso(loss_rows[0]["closed_at"]) if loss_rows else None,
            candle_staleness_bars=staleness,
            fee_rate=self.fee_rate(),
            slippage_rate=self.slippage_rate(),
            reconciliation=reconciliation,
            approved_quantity_cap=cap,
        )

    def preview(self, request: TradeIntentRequest) -> ValidationReport:
        """Dry-run validation: nothing is stored, no intent is created."""
        loaded = self.policy()
        expires = utc_now() + timedelta(minutes=loaded.policy.intent_ttl_minutes)
        return validate_intent(
            "preview", request, self._context(request, "preview", expires, None), "creation"
        )

    def create_intent(self, request: TradeIntentRequest) -> tuple[TradeIntent, ValidationReport, bool]:
        """Validate and store an intent. Returns (intent, report, replayed)."""
        existing = self.db.query("SELECT * FROM intents WHERE idempotency_key=?", (request.idempotency_key,))
        if existing:
            intent = self._row_to_intent(existing[0])
            report = ValidationReport.model_validate_json(existing[0]["creation_report_json"])
            return intent, report, True
        if not self.db.query("SELECT 1 FROM decisions WHERE decision_id=?", (request.decision_id,)):
            raise ExecutionError(f"unknown decision_id {request.decision_id}; record a decision report first")
        loaded = self.policy()
        now = utc_now()
        intent_id = new_id("int")
        expires = now + timedelta(minutes=loaded.policy.intent_ttl_minutes)
        report = validate_intent(
            intent_id, request, self._context(request, intent_id, expires, None), "creation"
        )
        status = IntentStatus.PENDING_APPROVAL if report.approved else IntentStatus.REJECTED
        quantity = report.sizing.quantity if report.approved and report.sizing else None
        intent = TradeIntent(
            intent_id=intent_id,
            trace_id=new_id("trace"),
            request=request,
            status=status,
            quantity=quantity,
            created_at=now,
            expires_at=expires,
            mode=self.mode.value,
            client_order_id="ct-" + intent_id.removeprefix("int_"),
        )
        self.db.execute(
            "INSERT INTO intents(intent_id, trace_id, decision_id, symbol, side, status, quantity, mode, client_order_id, idempotency_key, "
            "created_at, expires_at, request_json, creation_report_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent.intent_id,
                intent.trace_id,
                request.decision_id,
                request.symbol,
                request.side.value,
                status.value,
                format(quantity, "f") if quantity is not None else None,
                intent.mode,
                intent.client_order_id,
                request.idempotency_key,
                iso(now),
                iso(expires),
                request.model_dump_json(),
                report.model_dump_json(),
            ),  # fmt: skip
        )
        self.db.journal_append(intent_id, "intent_created", {"intent": intent, "validation": report})
        return intent, report, False

    def cancel_intent(self, intent_id: str, reason: str) -> bool:
        changed = self._set_status(intent_id, IntentStatus.CANCELLED, expected=IntentStatus.PENDING_APPROVAL)
        if changed:
            self.db.journal_append(intent_id, "intent_cancelled", {"reason": reason[:300]})
        return changed

    # ------------------------------------------------------------------ approvals

    def record_cli_approval(self, intent_id: str) -> dict[str, Any]:
        """Called only by the TTY-gated CLI after the operator typed the confirmation."""
        intent = self.get_intent(intent_id)
        if intent is None:
            raise ExecutionError("unknown intent")
        if intent.status is not IntentStatus.PENDING_APPROVAL:
            raise ExecutionError(f"intent is {intent.status.value}, not PENDING_APPROVAL")
        if utc_now() >= intent.expires_at:
            raise ExecutionError("intent has expired")
        return self._insert_approval(intent, "operator_cli_tty")

    def _insert_approval(self, intent: TradeIntent, method: str) -> dict[str, Any]:
        policy = self.policy().policy
        now = utc_now()
        approval = {
            "approval_id": new_id("apr"),
            "intent_id": intent.intent_id,
            "params_hash": approval_params_hash(intent),
            "method": method,
            "created_at": iso(now),
            "expires_at": iso(min(intent.expires_at, now + timedelta(minutes=policy.approval_ttl_minutes))),
        }
        self.db.execute(
            "INSERT INTO approvals(approval_id, intent_id, params_hash, method, created_at, expires_at) VALUES (:approval_id,:intent_id,:params_hash,:method,:created_at,:expires_at)",
            approval,
        )
        self.db.journal_append(intent.intent_id, "approval_recorded", approval)
        return approval

    def _usable_approval(self, intent: TradeIntent) -> str | None:
        rows = self.db.query(
            "SELECT approval_id FROM approvals WHERE intent_id=? AND params_hash=? AND consumed_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 1",
            (intent.intent_id, approval_params_hash(intent), iso(utc_now())),
        )
        return rows[0]["approval_id"] if rows else None

    def _consume_approval(self, approval_id: str) -> bool:
        return (
            self.db.execute(
                "UPDATE approvals SET consumed_at=? WHERE approval_id=? AND consumed_at IS NULL",
                (iso(utc_now()), approval_id),
            )
            == 1
        )

    # ------------------------------------------------------------------ execution

    def execute(self, intent_id: str) -> dict[str, Any]:
        intent = self.get_intent(intent_id)
        if intent is None:
            return {"status": "error", "reason": "unknown_intent"}
        if intent.mode != self.mode.value:
            return {
                "status": "rejected",
                "reason": f"intent was created in {intent.mode}, current mode is {self.mode.value}",
            }
        if intent.status is IntentStatus.FILLED:
            return {
                "status": "ok",
                "replayed": True,
                "intent_status": "FILLED",
                "detail": "already executed; nothing was sent again",
            }
        if intent.status is IntentStatus.UNKNOWN:
            return {"status": "rejected", "intent_status": "UNKNOWN", **self.reconcile(intent_id)}
        if intent.status is not IntentStatus.PENDING_APPROVAL:
            return {"status": "rejected", "reason": f"intent is {intent.status.value}"}
        if utc_now() >= intent.expires_at:
            self._set_status(intent_id, IntentStatus.EXPIRED, expected=IntentStatus.PENDING_APPROVAL)
            return {"status": "rejected", "reason": "intent expired; create a new one from fresh analysis"}

        policy = self.policy()
        cli_required = policy.policy.cli_approval_required(self.mode)
        approval_id = self._usable_approval(intent)
        if cli_required and approval_id is None:
            return {
                "status": "rejected",
                "reason": "awaiting_cli_approval",
                "detail": f"The operator must run `python -m trader approve {intent_id}` in a terminal. Claude cannot do this.",
            }
        if not self._set_status(intent_id, IntentStatus.EXECUTING, expected=IntentStatus.PENDING_APPROVAL):
            return {"status": "rejected", "reason": "intent state changed concurrently; nothing was sent"}

        request = intent.request
        try:
            context = self._context(request, intent_id, intent.expires_at, intent.quantity)
            report = validate_intent(intent_id, request, context, "execution")
        except Exception as exc:  # e.g. policy file unreadable -> fail closed
            self._set_status(intent_id, IntentStatus.REJECTED, expected=IntentStatus.EXECUTING)
            return {"status": "rejected", "reason": f"validation could not run: {type(exc).__name__}: {exc}"}
        self.db.execute(
            "UPDATE intents SET execution_report_json=? WHERE intent_id=?",
            (report.model_dump_json(), intent_id),
        )
        self.db.journal_append(intent_id, "execution_validation", report)
        if not report.approved or report.sizing is None or context.quote is None or context.filters is None:
            self._set_status(intent_id, IntentStatus.REJECTED, expected=IntentStatus.EXECUTING)
            return {
                "status": "rejected",
                "reason": "failed re-validation at execution time",
                "failed_checks": jsonable(report.failures),
            }

        if approval_id is None:  # PAPER: the Claude Code permission prompt on this tool is the human approval
            approval_id = self._insert_approval(intent, "claude_code_permission_prompt")["approval_id"]
        if not self._consume_approval(approval_id):
            self._set_status(intent_id, IntentStatus.REJECTED, expected=IntentStatus.EXECUTING)
            return {"status": "rejected", "reason": "approval already used"}

        quantity = report.sizing.quantity
        broker = self.broker()
        if request.side is Side.SELL:
            self._cancel_protective_stop(request.symbol)
        self.db.journal_append(
            intent_id,
            "order_submitted",
            {
                "client_order_id": intent.client_order_id,
                "quantity": quantity,
                "side": request.side.value,
                "type": request.order_type.value,
            },
        )
        try:
            result = broker.execute(
                symbol=request.symbol, side=request.side, order_type=request.order_type, quantity=quantity,
                limit_price=request.limit_price, client_order_id=intent.client_order_id, quote=context.quote,
            )  # fmt: skip
        except OrderStateUnknown as exc:
            self._set_status(intent_id, IntentStatus.UNKNOWN, expected=IntentStatus.EXECUTING)
            self.db.journal_append(intent_id, "order_state_unknown", {"detail": str(exc)})
            return {
                "status": "error",
                "intent_status": "UNKNOWN",
                "detail": "Order outcome unknown. Nothing will be resent.",
                **self.reconcile(intent_id),
            }
        except BinanceAPIError as exc:
            self._set_status(intent_id, IntentStatus.FAILED, expected=IntentStatus.EXECUTING)
            self.db.journal_append(intent_id, "order_rejected", {"detail": str(exc)})
            return {"status": "rejected", "intent_status": "FAILED", "reason": str(exc)}
        return self._apply_result(intent, result, context.filters)

    def reconcile(self, intent_id: str) -> dict[str, Any]:
        """Resolve an UNKNOWN intent by asking the exchange for the order by its client id."""
        intent = self.get_intent(intent_id)
        if intent is None or intent.status is not IntentStatus.UNKNOWN:
            return {"reconciliation": "nothing_to_reconcile"}
        try:
            found = self.broker().find_order(intent.request.symbol, intent.client_order_id)
        except Exception as exc:
            return {"reconciliation": "still_unknown", "detail": f"query failed: {type(exc).__name__}"}
        if found is None:
            unknown_since = [
                e for e in self.db.journal_events(intent_id) if e["event_type"] == "order_state_unknown"
            ]
            age = (utc_now() - parse_iso(unknown_since[-1]["ts"])).total_seconds() if unknown_since else 0
            if age >= 30:
                self._set_status(intent_id, IntentStatus.FAILED, expected=IntentStatus.UNKNOWN)
                return {
                    "reconciliation": "resolved_not_on_exchange",
                    "detail": "exchange reports no such order after 30s; marked FAILED",
                }
            return {
                "reconciliation": "still_unknown",
                "detail": "order not found yet; query again in 30s before concluding",
            }
        if found.executed_qty > 0 or found.status in {"REJECTED", "CANCELED", "EXPIRED"}:
            self.db.execute(
                "UPDATE intents SET status=? WHERE intent_id=? AND status=?",
                (IntentStatus.EXECUTING.value, intent_id, IntentStatus.UNKNOWN.value),
            )
            outcome = self._apply_result(intent, found, self.market.filters(intent.request.symbol))
            return {"reconciliation": "resolved", "outcome": outcome}
        return {
            "reconciliation": "still_unknown",
            "detail": f"order exists with status {found.status} and no fills yet",
        }

    def _record_order(self, intent_id: str, role: str, result: OrderResult) -> None:
        now = iso(utc_now())
        self.db.execute(
            "INSERT INTO orders(client_order_id, intent_id, mode, role, symbol, side, order_type, status, requested_qty, executed_qty, avg_price, "
            "exchange_order_id, created_at, updated_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET status=excluded.status, executed_qty=excluded.executed_qty, avg_price=excluded.avg_price, "
            "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (
                result.client_order_id,
                intent_id,
                self.mode.value,
                role,
                result.symbol,
                result.side.value,
                result.order_type.value,
                result.status,
                format(result.requested_qty, "f"),
                format(result.executed_qty, "f"),
                format(result.avg_price, "f") if result.avg_price is not None else None,
                result.exchange_order_id,
                now,
                now,
                result.model_dump_json(),
            ),  # fmt: skip
        )
        if not self.db.query(
            "SELECT 1 FROM fills WHERE client_order_id=? LIMIT 1", (result.client_order_id,)
        ):
            for fill in result.fills:
                self.db.execute(
                    "INSERT INTO fills(client_order_id, ts, price, quantity, fee, fee_asset) VALUES (?,?,?,?,?,?)",
                    (
                        result.client_order_id,
                        iso(fill.ts),
                        format(fill.price, "f"),
                        format(fill.quantity, "f"),
                        format(fill.fee, "f"),
                        fill.fee_asset,
                    ),
                )

    def _apply_result(
        self, intent: TradeIntent, result: OrderResult, filters: SymbolFilters
    ) -> dict[str, Any]:
        request = intent.request
        self._record_order(intent.intent_id, "entry" if request.side is Side.BUY else "exit", result)
        self.db.journal_append(intent.intent_id, "order_result", result)
        if result.executed_qty <= 0 or result.avg_price is None:
            self._set_status(intent.intent_id, IntentStatus.FAILED, expected=IntentStatus.EXECUTING)
            return {
                "status": "rejected",
                "intent_status": "FAILED",
                "order_status": result.status,
                "detail": result.detail or "no quantity was filled",
            }

        base_fee = sum((f.fee for f in result.fills if f.fee_asset == filters.base_asset), Decimal(0))
        quote_fee = sum((f.fee for f in result.fills if f.fee_asset == filters.quote_asset), Decimal(0))
        other_fee_assets = sorted(
            {
                f.fee_asset
                for f in result.fills
                if f.fee_asset not in (filters.base_asset, filters.quote_asset, "")
            }
        )
        fees_in_quote = quote_fee + base_fee * result.avg_price
        slippage_bps = None
        if result.reference_price:
            sign = Decimal(1) if request.side is Side.BUY else Decimal(-1)
            slippage_bps = sign * (result.avg_price - result.reference_price) / result.reference_price * BPS
        warnings = [f"fees paid in {a} are not converted and are excluded from PnL" for a in other_fee_assets]
        if result.executed_qty < result.requested_qty:
            warnings.append(f"partial fill: {result.executed_qty} of {result.requested_qty}")

        summary: dict[str, Any] = {
            "status": "ok",
            "intent_status": "FILLED",
            "mode": self.mode.value,
            "order": {
                "client_order_id": result.client_order_id,
                "side": request.side.value,
                "executed_qty": result.executed_qty,
                "avg_price": result.avg_price,
                "fees_in_quote": fees_in_quote,
            },
            "execution_quality": {
                "reference_price": result.reference_price,
                "realized_slippage_bps": slippage_bps,
                "assumed_slippage_bps": self.slippage_rate() * BPS,
            },
            "warnings": warnings,
        }
        if request.side is Side.BUY:
            held = result.executed_qty - base_fee
            self.db.execute(
                "INSERT INTO positions(mode, symbol, quantity, avg_entry, stop_price, take_profit, opened_at, intent_id, last_checked_ms, entry_fees) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    self.mode.value,
                    request.symbol,
                    format(held, "f"),
                    format(result.avg_price, "f"),
                    format(request.stop_price, "f") if request.stop_price is not None else None,
                    format(request.take_profit, "f") if request.take_profit is not None else None,
                    iso(utc_now()),
                    intent.intent_id,
                    to_ms(utc_now()),
                    format(fees_in_quote, "f"),
                ),  # fmt: skip
            )
            self.db.journal_append(
                intent.intent_id,
                "position_opened",
                {
                    "symbol": request.symbol,
                    "quantity": held,
                    "avg_entry": result.avg_price,
                    "stop": request.stop_price,
                    "target": request.take_profit,
                },
            )
            summary["protective_stop"] = self._place_stop(intent, held, filters)
        else:
            summary["closed"] = self._reduce_position(
                request.symbol,
                result.executed_qty,
                result.avg_price,
                fees_in_quote,
                "operator_exit",
                intent.intent_id,
            )
        self._set_status(intent.intent_id, IntentStatus.FILLED, expected=IntentStatus.EXECUTING)
        return jsonable(summary)

    def _place_stop(self, intent: TradeIntent, held: Decimal, filters: SymbolFilters) -> dict[str, Any]:
        stop = intent.request.stop_price
        if stop is None:
            return {"status": "none", "detail": "no stop configured"}
        if self.mode is TradingMode.PAPER:
            return {
                "status": "simulated",
                "detail": "paper stop is evaluated against 1m/1h bars whenever positions are reviewed; nothing watches it between sessions",
            }
        quantity = round_quantity(held, filters, OrderType.LIMIT)
        client_id = intent.client_order_id + "-s"
        try:
            placed = self.broker().place_protective_stop(
                symbol=intent.request.symbol,
                quantity=quantity,
                stop_price=stop,
                filters=filters,
                client_order_id=client_id,
            )
        except Exception as exc:
            self.db.journal_append(intent.intent_id, "protective_stop_failed", {"detail": str(exc)})
            return {
                "status": "FAILED",
                "detail": f"POSITION IS OPEN WITHOUT AN EXCHANGE STOP: {type(exc).__name__}: {exc}. Tell the operator immediately.",
            }
        if placed is not None:
            self._record_order(intent.intent_id, "stop", placed)
            self.db.journal_append(intent.intent_id, "protective_stop_placed", placed)
        return {"status": "placed", "client_order_id": client_id, "trigger": stop}

    def _cancel_protective_stop(self, symbol: str) -> None:
        if self.mode is TradingMode.PAPER:
            return
        rows = self.db.query(
            "SELECT client_order_id FROM orders WHERE mode=? AND symbol=? AND role='stop' AND status='NEW'",
            (self.mode.value, symbol),
        )
        for row in rows:
            try:
                self.broker().cancel(symbol, row["client_order_id"])
                self.db.execute(
                    "UPDATE orders SET status='CANCELED', updated_at=? WHERE client_order_id=?",
                    (iso(utc_now()), row["client_order_id"]),
                )
            except BinanceAPIError:
                continue  # already filled or gone; reconciliation in monitor_positions() will settle it

    def _reduce_position(
        self,
        symbol: str,
        quantity: Decimal,
        exit_price: Decimal,
        exit_fees: Decimal,
        reason: str,
        via_intent: str | None,
    ) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM positions WHERE mode=? AND symbol=?", (self.mode.value, symbol))
        if not rows:
            raise ExecutionError(f"no position in {symbol} to reduce")
        row = rows[0]
        held, entry, entry_fees = (
            Decimal(row["quantity"]),
            Decimal(row["avg_entry"]),
            Decimal(row["entry_fees"]),
        )
        sold = min(quantity, held)
        share = sold / held if held > 0 else Decimal(1)
        fees = exit_fees + entry_fees * share
        pnl = (exit_price - entry) * sold - fees
        now = iso(utc_now())
        self.db.execute(
            "INSERT INTO closed_trades(mode, symbol, intent_id, opened_at, closed_at, quantity, avg_entry, avg_exit, fees, realized_pnl, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.mode.value,
                symbol,
                row["intent_id"],
                row["opened_at"],
                now,
                format(sold, "f"),
                format(entry, "f"),
                format(exit_price, "f"),
                format(fees, "f"),
                format(pnl, "f"),
                reason,
            ),
        )
        remaining = held - sold
        if remaining <= 0:
            self.db.execute("DELETE FROM positions WHERE mode=? AND symbol=?", (self.mode.value, symbol))
        else:
            self.db.execute(
                "UPDATE positions SET quantity=?, entry_fees=? WHERE mode=? AND symbol=?",
                (
                    format(remaining, "f"),
                    format(entry_fees - entry_fees * share, "f"),
                    self.mode.value,
                    symbol,
                ),
            )
        closed = {
            "symbol": symbol,
            "quantity": sold,
            "avg_entry": entry,
            "avg_exit": exit_price,
            "fees": fees,
            "realized_pnl": pnl,
            "exit_reason": reason,
            "remaining": max(remaining, Decimal(0)),
            "via_intent": via_intent,
        }
        self.db.journal_append(
            row["intent_id"], "position_closed" if remaining <= 0 else "position_reduced", closed
        )
        return closed

    # ------------------------------------------------------------------ monitoring

    def monitor_positions(self) -> list[dict[str, Any]]:
        """Settle stops/targets that triggered since the last check. Returns the events that occurred."""
        events: list[dict[str, Any]] = []
        for position in self.positions():
            try:
                event = (
                    self._monitor_paper(position)
                    if self.mode is TradingMode.PAPER
                    else self._monitor_exchange(position)
                )
            except Exception as exc:
                event = {
                    "symbol": position.symbol,
                    "status": "monitor_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            if event:
                events.append(jsonable(event))
        return events

    def _monitor_paper(self, position: Position) -> dict[str, Any] | None:
        if position.stop_price is None and position.take_profit is None:
            return None
        now_ms = to_ms(utc_now())
        gap = now_ms - position.last_checked_ms
        interval = "1m" if gap <= 16 * 3_600_000 else "1h"
        rows = self.market.source.klines(
            position.symbol, interval, start_ms=position.last_checked_ms, limit=1000
        )
        frame = drop_unclosed(klines_to_frame(rows), now_ms)
        slip = self.slippage_rate()
        for bar in frame.itertuples(index=False):
            low, high, open_ = Decimal(str(bar.low)), Decimal(str(bar.high)), Decimal(str(bar.open))
            exit_price: Decimal | None = None
            reason = ""
            if position.stop_price is not None and low <= position.stop_price:
                # stop is assumed to trigger before a target inside the same bar; a gap fills at the open
                exit_price, reason = min(open_, position.stop_price) * (1 - slip), "stop"
            elif position.take_profit is not None and high >= position.take_profit:
                exit_price, reason = position.take_profit, "target"
            if exit_price is not None:
                notional = position.quantity * exit_price
                fee = notional * self.fee_rate()
                quote_asset = self.app.universe["quote_asset"]
                free = self.broker().free_quote(quote_asset)
                self.db.execute(
                    "UPDATE paper_balances SET free=? WHERE asset=?",
                    (format(free + notional - fee, "f"), quote_asset),
                )
                closed = self._reduce_position(
                    position.symbol, position.quantity, exit_price, fee, reason, None
                )
                return {
                    "status": "closed",
                    "bar_interval": interval,
                    "bar_open_time": iso(from_ms(int(str(bar.open_time)))),
                    **closed,
                }
        if not frame.empty:
            self.db.execute(
                "UPDATE positions SET last_checked_ms=? WHERE mode=? AND symbol=?",
                (int(frame["close_time"].iloc[-1]) + 1, self.mode.value, position.symbol),
            )
        return None

    def _monitor_exchange(self, position: Position) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT client_order_id FROM orders WHERE mode=? AND symbol=? AND role='stop' AND status IN ('NEW','PARTIALLY_FILLED')",
            (self.mode.value, position.symbol),
        )
        for row in rows:
            order = self.broker().find_order(position.symbol, row["client_order_id"])
            if order is None:
                continue
            self._record_order(position.intent_id, "stop", order)
            if order.executed_qty > 0 and order.avg_price is not None:
                filters = self.market.filters(position.symbol)
                fees = sum(
                    (
                        f.fee if f.fee_asset == filters.quote_asset else f.fee * order.avg_price
                        for f in order.fills
                        if f.fee_asset in (filters.quote_asset, filters.base_asset)
                    ),
                    Decimal(0),
                )
                closed = self._reduce_position(
                    position.symbol, order.executed_qty, order.avg_price, fees, "stop", None
                )
                return {"status": "closed", **closed}
        return None

    def closed_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM closed_trades WHERE mode=? ORDER BY id DESC LIMIT ?", (self.mode.value, limit)
        )

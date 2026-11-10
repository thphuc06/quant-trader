"""Binance Spot symbol filters (PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, NOTIONAL / MIN_NOTIONAL)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from trader.domain import OrderType, SymbolFilters


class FilterError(ValueError):
    """An order would violate an exchange filter."""


def parse_symbol_filters(symbol_info: dict[str, Any], fetched_at: datetime) -> SymbolFilters:
    filters = {f["filterType"]: f for f in symbol_info.get("filters", [])}
    try:
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
    except KeyError as exc:
        raise FilterError(f"{symbol_info.get('symbol')}: missing filter {exc}") from exc
    market_lot = filters.get("MARKET_LOT_SIZE")
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
    if notional is None:
        raise FilterError(f"{symbol_info.get('symbol')}: missing NOTIONAL/MIN_NOTIONAL filter")
    max_notional = notional.get("maxNotional")

    def positive_or_none(raw: str | None) -> Decimal | None:
        if raw is None:
            return None
        value = Decimal(raw)
        return value if value > 0 else None

    return SymbolFilters(
        symbol=symbol_info["symbol"],
        status=symbol_info.get("status", "UNKNOWN"),
        base_asset=symbol_info["baseAsset"],
        quote_asset=symbol_info["quoteAsset"],
        tick_size=Decimal(price["tickSize"]),
        min_price=Decimal(price["minPrice"]),
        max_price=Decimal(price["maxPrice"]),
        step_size=Decimal(lot["stepSize"]),
        min_qty=Decimal(lot["minQty"]),
        max_qty=Decimal(lot["maxQty"]),
        market_step_size=positive_or_none(market_lot["stepSize"]) if market_lot else None,
        market_min_qty=positive_or_none(market_lot["minQty"]) if market_lot else None,
        market_max_qty=positive_or_none(market_lot["maxQty"]) if market_lot else None,
        min_notional=Decimal(notional["minNotional"]),
        max_notional=positive_or_none(max_notional),
        fetched_at=fetched_at,
    )


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_quantity(quantity: Decimal, filters: SymbolFilters, order_type: OrderType) -> Decimal:
    """Round DOWN to the step size. Never rounds up: that would exceed the risk budget."""
    step = filters.step_size
    if order_type is OrderType.MARKET and filters.market_step_size is not None:
        step = max(step, filters.market_step_size)
    return _floor_to_step(quantity, step).normalize() if quantity > 0 else Decimal("0")


def round_price(price: Decimal, filters: SymbolFilters) -> Decimal:
    return _floor_to_step(price, filters.tick_size)


def check_order(
    quantity: Decimal, price: Decimal, filters: SymbolFilters, order_type: OrderType
) -> list[str]:
    """Return human-readable violations (empty list = order satisfies the filters)."""
    problems: list[str] = []
    if filters.status != "TRADING":
        problems.append(f"symbol status is {filters.status}, not TRADING")
    min_qty, max_qty, step = filters.min_qty, filters.max_qty, filters.step_size
    if order_type is OrderType.MARKET:
        if filters.market_min_qty is not None:
            min_qty = max(min_qty, filters.market_min_qty)
        if filters.market_max_qty is not None:
            max_qty = min(max_qty, filters.market_max_qty)
        if filters.market_step_size is not None:
            step = max(step, filters.market_step_size)
    if quantity < min_qty:
        problems.append(f"quantity {quantity} below minQty {min_qty}")
    if quantity > max_qty:
        problems.append(f"quantity {quantity} above maxQty {max_qty}")
    if step > 0 and quantity % step != 0:
        problems.append(f"quantity {quantity} is not a multiple of stepSize {step}")
    if order_type is OrderType.LIMIT:
        if filters.min_price > 0 and price < filters.min_price:
            problems.append(f"price {price} below minPrice {filters.min_price}")
        if filters.max_price > 0 and price > filters.max_price:
            problems.append(f"price {price} above maxPrice {filters.max_price}")
        if filters.tick_size > 0 and price % filters.tick_size != 0:
            problems.append(f"price {price} is not a multiple of tickSize {filters.tick_size}")
    notional = quantity * price
    if notional < filters.min_notional:
        problems.append(f"notional {notional} below minNotional {filters.min_notional}")
    if filters.max_notional is not None and notional > filters.max_notional:
        problems.append(f"notional {notional} above maxNotional {filters.max_notional}")
    return problems

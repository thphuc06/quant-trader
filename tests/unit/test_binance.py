"""Contract tests for the Binance adapters against mocked HTTP. No network, no real endpoint is called."""

from __future__ import annotations

import hashlib
import hmac
import inspect
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from pydantic import SecretStr

from trader.config import HttpConfig
from trader.domain import OrderType, Side
from trader.exchanges.binance import client as client_module
from trader.exchanges.binance.client import (
    BinanceAPIError,
    BinancePublicClient,
    BinanceTradingClient,
    OrderStateUnknown,
    RateLimited,
)
from trader.exchanges.binance.filters import FilterError, check_order, parse_symbol_filters, round_quantity
from trader.exchanges.binance.stream import parse_kline_message, stream_url
from trader.timeutil import utc_now

CFG = HttpConfig(timeout_seconds=1, max_retries=2)
KLINE = [
    1499040000000,
    "0.01634790",
    "0.80000000",
    "0.01575800",
    "0.01577100",
    "148976.11427815",
    1499644799999,
    "2434.19055334",
    308,
    "1756.87402397",
    "28.46694368",
    "0",
]
SYMBOL_INFO = {
    "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "100", "stepSize": "0"},
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True, "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5},
    ],
}  # fmt: skip


def public(handler) -> BinancePublicClient:
    return BinancePublicClient(
        "https://data.test",
        "https://fut.test",
        CFG,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def trading(handler) -> BinanceTradingClient:
    return BinanceTradingClient(
        "https://demo.test",
        SecretStr("key-abc"),
        SecretStr("secret-xyz"),
        CFG,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        now_ms=lambda: 1_700_000_000_000,
    )


def test_klines_and_quote_parse_documented_shapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/klines":
            assert dict(request.url.params) == {"symbol": "BTCUSDT", "interval": "1h", "limit": "2"}
            return httpx.Response(200, json=[KLINE, KLINE], headers={"x-mbx-used-weight-1m": "7"})
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "bidPrice": "100.10",
                "bidQty": "1",
                "askPrice": "100.20",
                "askQty": "2",
            },
        )

    c = public(handler)
    assert len(c.klines("btcusdt", "1h", limit=2)) == 2 and c.used_weight_1m == 7
    quote = c.quote("BTCUSDT")
    assert quote.bid == Decimal("100.10") and quote.spread_bps > 0 and quote.observed_at.tzinfo is not None
    with pytest.raises(ValueError):
        c.klines("BTCUSDT", "1h", limit=5000)


def test_429_backs_off_and_does_not_hit_the_api_again() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            429, headers={"retry-after": "30"}, json={"code": -1003, "msg": "Too many requests"}
        )

    c = public(handler)
    with pytest.raises(RateLimited) as first:
        c.klines("BTCUSDT", "1h")
    assert first.value.retry_after == 30 and not first.value.banned
    with pytest.raises(RateLimited):
        c.klines("BTCUSDT", "1h")
    assert len(calls) == 1  # the second call was refused locally


def test_418_is_reported_as_ban() -> None:
    with pytest.raises(RateLimited) as exc:
        public(lambda r: httpx.Response(418, headers={"retry-after": "120"})).klines("BTCUSDT", "1h")
    assert exc.value.banned


def test_get_is_retried_on_server_error_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] < 3 else httpx.Response(200, json=[KLINE])

    assert len(public(handler).klines("BTCUSDT", "1h")) == 1 and attempts["n"] == 3


def test_local_soft_weight_limit_pauses_requests() -> None:
    c = BinancePublicClient(
        "https://d.test",
        "https://f.test",
        HttpConfig(weight_soft_limit_1m=10),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[], headers={"x-mbx-used-weight-1m": "11"})
        ),
    )
    c.klines("BTCUSDT", "1h")
    with pytest.raises(RateLimited):
        c.klines("BTCUSDT", "1h")


def test_signed_request_has_valid_hmac_and_key_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_500})
        seen.append(request)
        return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": "10", "locked": "0"}]})

    trading(handler).account()
    request = seen[0]
    params = parse_qsl(request.url.query.decode())
    signature = dict(params)["signature"]
    unsigned = urlencode([(k, v) for k, v in params if k != "signature"])
    assert signature == hmac.new(b"secret-xyz", unsigned.encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-MBX-APIKEY"] == "key-abc"
    assert dict(params)["timestamp"] == "1700000000500"  # local clock corrected by server offset
    assert dict(params)["recvWindow"] == "5000"
    assert "secret-xyz" not in str(request.url)


def _order_handler(order_response: httpx.Response | Exception, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        calls.append(f"{request.method} {request.url.path}")
        if isinstance(order_response, Exception):
            raise order_response
        return order_response

    return handler


def test_order_timeout_is_unknown_state_and_never_retried() -> None:
    calls: list[str] = []
    c = trading(_order_handler(httpx.ReadTimeout("timeout"), calls))
    with pytest.raises(OrderStateUnknown):
        c.new_order(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type="MARKET",
            quantity=Decimal("0.01"),
            client_order_id="ct-1",
        )
    assert calls == ["POST /api/v3/order"]


def test_order_5xx_is_unknown_state_but_4xx_is_a_definite_rejection() -> None:
    calls: list[str] = []
    with pytest.raises(OrderStateUnknown):
        trading(_order_handler(httpx.Response(502), calls)).new_order(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type="MARKET",
            quantity=Decimal("0.01"),
            client_order_id="ct-1",
        )
    assert calls == ["POST /api/v3/order"]
    with pytest.raises(BinanceAPIError) as exc:
        trading(
            _order_handler(
                httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance"}), []
            )
        ).new_order(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type="MARKET",
            quantity=Decimal("0.01"),
            client_order_id="ct-1",
        )
    assert exc.value.code == -2010 and not isinstance(exc.value, RateLimited)


def test_order_response_is_parsed_with_fills_and_average_price() -> None:
    body = {
        "symbol": "BTCUSDT", "orderId": 28, "clientOrderId": "ct-1", "transactTime": 1507725176595, "origQty": "0.02", "executedQty": "0.02",
        "cummulativeQuoteQty": "2000.00", "status": "FILLED", "type": "MARKET", "side": "BUY",
        "fills": [{"price": "100000", "qty": "0.01", "commission": "0.00001", "commissionAsset": "BTC"}, {"price": "100000", "qty": "0.01", "commission": "0.00001", "commissionAsset": "BTC"}],
    }  # fmt: skip
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        seen.append(request)
        return httpx.Response(200, json=body)

    result = trading(handler).new_order(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type="MARKET",
        quantity=Decimal("0.020"),
        client_order_id="ct-1",
    )
    assert result.status == "FILLED" and result.avg_price == Decimal("100000") and len(result.fills) == 2
    sent = dict(parse_qsl(seen[0].url.query.decode()))
    assert (
        sent["newClientOrderId"] == "ct-1"
        and sent["quantity"] == "0.020"
        and sent["newOrderRespType"] == "FULL"
    )


def test_get_order_returns_none_only_for_order_does_not_exist() -> None:
    assert (
        trading(
            _order_handler(httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}), [])
        ).get_order("BTCUSDT", "ct-1")
        is None
    )
    with pytest.raises(BinanceAPIError):
        trading(
            _order_handler(httpx.Response(400, json={"code": -1022, "msg": "Signature invalid"}), [])
        ).get_order("BTCUSDT", "ct-1")


def test_trading_client_has_no_generic_or_fund_moving_methods() -> None:
    names = {
        name
        for name, _ in inspect.getmembers(BinanceTradingClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert names == {"close", "account", "get_order", "open_orders", "new_order", "cancel_order"}
    source = inspect.getsource(client_module).lower()
    for forbidden in ("withdraw", "/sapi/", "transfer", "margin", "leverage"):
        assert forbidden not in source.replace("no withdraw/transfer/margin endpoint", "")


# ----------------------------------------------------------------------------- filters


def test_filters_parse_and_round_down() -> None:
    filters = parse_symbol_filters(SYMBOL_INFO, utc_now())
    assert (
        filters.step_size == Decimal("0.00001")
        and filters.market_step_size is None
        and filters.market_max_qty == Decimal("100")
    )
    assert round_quantity(Decimal("0.123456789"), filters, OrderType.MARKET) == Decimal("0.12345")
    assert round_quantity(Decimal("0.000009"), filters, OrderType.MARKET) == 0
    assert check_order(Decimal("0.12345"), Decimal("100000"), filters, OrderType.MARKET) == []
    assert any(
        "minNotional" in p for p in check_order(Decimal("0.00001"), Decimal("100"), filters, OrderType.MARKET)
    )
    assert any(
        "stepSize" in p
        for p in check_order(Decimal("0.123456"), Decimal("100000"), filters, OrderType.MARKET)
    )
    assert any(
        "maxQty" in p for p in check_order(Decimal("150"), Decimal("100000"), filters, OrderType.MARKET)
    )
    assert any(
        "tickSize" in p for p in check_order(Decimal("0.1"), Decimal("100000.001"), filters, OrderType.LIMIT)
    )
    halted = filters.model_copy(update={"status": "HALT"})
    assert any(
        "TRADING" in p for p in check_order(Decimal("0.1"), Decimal("100000"), halted, OrderType.MARKET)
    )


def test_missing_filters_are_an_error_not_a_default() -> None:
    with pytest.raises(FilterError):
        parse_symbol_filters(
            {"symbol": "X", "baseAsset": "X", "quoteAsset": "USDT", "filters": []}, utc_now()
        )


# ----------------------------------------------------------------------------- stream


def test_kline_stream_only_accepts_closed_klines() -> None:
    k = {
        "t": 1,
        "T": 2,
        "s": "BTCUSDT",
        "i": "1h",
        "o": "1",
        "c": "2",
        "h": "3",
        "l": "0.5",
        "v": "10",
        "n": 5,
        "x": True,
        "q": "20",
        "V": "4",
        "Q": "8",
    }
    import json

    closed = parse_kline_message(json.dumps({"stream": "btcusdt@kline_1h", "data": {"e": "kline", "k": k}}))
    assert closed is not None and closed["close"] == 2.0 and closed["symbol"] == "BTCUSDT"
    assert parse_kline_message(json.dumps({"e": "kline", "k": {**k, "x": False}})) is None
    assert parse_kline_message("not json") is None
    assert parse_kline_message(json.dumps({"e": "kline"})) is None
    assert (
        stream_url("wss://s.test:9443", ["BTCUSDT", "ETHUSDT"], "1h")
        == "wss://s.test:9443/stream?streams=btcusdt@kline_1h/ethusdt@kline_1h"
    )

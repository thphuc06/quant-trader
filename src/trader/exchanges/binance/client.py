"""Binance Spot REST clients.

``BinancePublicClient`` needs no credentials and only calls market-data endpoints.
``BinanceTradingClient`` signs a *fixed* set of endpoints (account, order, query, cancel). There is
deliberately no generic "signed request" method and no withdraw/transfer/margin endpoint.

Endpoint facts are documented in research/binance-api-research.md.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from trader.config import HttpConfig
from trader.domain import Fill, OrderResult, OrderType, Quote, Side, SymbolFilters
from trader.exchanges.binance.filters import parse_symbol_filters
from trader.timeutil import from_ms, utc_now

ORDER_NOT_FOUND_CODE = -2013


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int | None, message: str) -> None:
        super().__init__(f"Binance API error http={status} code={code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class RateLimited(BinanceAPIError):
    def __init__(self, status: int, retry_after: float, message: str) -> None:
        super().__init__(status, None, message)
        self.retry_after = retry_after
        self.banned = status == 418


class OrderStateUnknown(Exception):
    """The request may or may not have reached the matching engine. Reconcile before any retry."""


class _Http:
    """Thin httpx wrapper: weight tracking, back-off on 429/418, bounded retries for idempotent GETs."""

    def __init__(
        self,
        base_url: str,
        config: HttpConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=config.timeout_seconds, transport=transport)
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._blocked_until = 0.0
        self.used_weight_1m: int | None = None

    def close(self) -> None:
        self._client.close()

    def _check_block(self) -> None:
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            raise RateLimited(429, remaining, f"backing off for {remaining:.0f}s after a rate-limit response")
        if self.used_weight_1m is not None and self.used_weight_1m >= self._config.weight_soft_limit_1m:
            self._blocked_until = self._clock() + 60
            self.used_weight_1m = None
            raise RateLimited(429, 60, "local soft weight limit reached; pausing 60s")

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        retry: bool,
    ) -> Any:
        attempts = self._config.max_retries + 1 if retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._check_block()
            try:
                response = self._client.request(method, path, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))
                    continue
                raise
            weight = response.headers.get("x-mbx-used-weight-1m")
            if weight is not None and weight.isdigit():
                self.used_weight_1m = int(weight)
            if response.status_code in (429, 418):
                retry_after = float(response.headers.get("retry-after", "60") or 60)
                self._blocked_until = self._clock() + retry_after
                raise RateLimited(
                    response.status_code, retry_after, "rate limited by Binance; do not retry early"
                )
            if response.status_code >= 500:
                last_error = BinanceAPIError(response.status_code, None, "server error")
                if attempt + 1 < attempts:
                    self._sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))
                    continue
                raise last_error
            if response.status_code >= 400:
                code, message = None, response.text[:300]
                try:
                    body = response.json()
                    code, message = body.get("code"), str(body.get("msg", message))
                except ValueError:
                    pass
                raise BinanceAPIError(response.status_code, code, message)
            return response.json()
        raise RuntimeError("unreachable") from last_error


class BinancePublicClient:
    """Public market data (security type NONE)."""

    def __init__(
        self,
        market_data_rest: str,
        futures_public_rest: str,
        config: HttpConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._spot = _Http(market_data_rest, config, transport, sleep)
        self._futures = _Http(futures_public_rest, config, transport, sleep)

    def close(self) -> None:
        self._spot.close()
        self._futures.close()

    @property
    def used_weight_1m(self) -> int | None:
        return self._spot.used_weight_1m

    def server_time_ms(self) -> int:
        return int(self._spot.request("GET", "/api/v3/time", retry=True)["serverTime"])

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be within 1..1000")
        params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self._spot.request("GET", "/api/v3/klines", params, retry=True)

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        data = self._spot.request("GET", "/api/v3/exchangeInfo", {"symbol": symbol.upper()}, retry=True)
        return parse_symbol_filters(data["symbols"][0], utc_now())

    def quote(self, symbol: str) -> Quote:
        data = self._spot.request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol.upper()}, retry=True)
        return Quote(
            symbol=data["symbol"],
            bid=Decimal(data["bidPrice"]),
            ask=Decimal(data["askPrice"]),
            bid_qty=Decimal(data["bidQty"]),
            ask_qty=Decimal(data["askQty"]),
            observed_at=utc_now(),
        )

    def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return self._spot.request("GET", "/api/v3/ticker/24hr", {"symbol": symbol.upper()}, retry=True)

    def futures_premium_index(self, symbol: str) -> dict[str, Any]:
        return self._futures.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol.upper()}, retry=True)

    def futures_open_interest(self, symbol: str) -> dict[str, Any]:
        return self._futures.request("GET", "/fapi/v1/openInterest", {"symbol": symbol.upper()}, retry=True)


class BinanceTradingClient:
    """Signed Spot endpoints for one environment (Demo or Live). HMAC-SHA256 signing."""

    def __init__(
        self,
        base_url: str,
        api_key: SecretStr,
        api_secret: SecretStr,
        config: HttpConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._http = _Http(base_url, config, transport, sleep)
        self._key = api_key
        self._secret = api_secret
        self._config = config
        self._now_ms = now_ms
        self._time_offset_ms: int | None = None

    def close(self) -> None:
        self._http.close()

    # -- signing ---------------------------------------------------------------

    def _sync_time(self) -> None:
        server = int(self._http.request("GET", "/api/v3/time", retry=True)["serverTime"])
        self._time_offset_ms = server - self._now_ms()

    def _signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._time_offset_ms is None:
            self._sync_time()
        assert self._time_offset_ms is not None
        payload = dict(params)
        payload["recvWindow"] = self._config.recv_window_ms
        payload["timestamp"] = self._now_ms() + self._time_offset_ms
        query = urlencode(payload)
        signature = hmac.new(
            self._secret.get_secret_value().encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        payload["signature"] = signature
        return payload

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._key.get_secret_value()}

    # -- read ------------------------------------------------------------------

    def account(self) -> dict[str, Any]:
        params = self._signed_params({"omitZeroBalances": "true"})
        return self._http.request("GET", "/api/v3/account", params, self._headers(), retry=True)

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        """Return the order, or None when the exchange states it does not exist."""
        params = self._signed_params({"symbol": symbol.upper(), "origClientOrderId": client_order_id})
        try:
            data = self._http.request("GET", "/api/v3/order", params, self._headers(), retry=True)
        except BinanceAPIError as exc:
            if exc.code == ORDER_NOT_FOUND_CODE:
                return None
            raise
        return _parse_order(data)

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        params = self._signed_params({"symbol": symbol.upper()})
        return self._http.request("GET", "/api/v3/openOrders", params, self._headers(), retry=True)

    # -- write (never retried blindly) -----------------------------------------

    def new_order(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: str,
        quantity: Decimal,
        client_order_id: str,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        time_in_force: str | None = None,
    ) -> OrderResult:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.value,
            "type": order_type,
            "quantity": format(quantity, "f"),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if price is not None:
            params["price"] = format(price, "f")
        if stop_price is not None:
            params["stopPrice"] = format(stop_price, "f")
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        try:
            data = self._http.request(
                "POST", "/api/v3/order", self._signed_params(params), self._headers(), retry=False
            )
        except RateLimited:
            raise
        except BinanceAPIError as exc:
            if exc.status >= 500:
                raise OrderStateUnknown(f"server error {exc.status} on order {client_order_id}") from exc
            raise
        except httpx.TransportError as exc:
            raise OrderStateUnknown(
                f"transport error on order {client_order_id}: {type(exc).__name__}"
            ) from exc
        return _parse_order(data)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        params = self._signed_params({"symbol": symbol.upper(), "origClientOrderId": client_order_id})
        return self._http.request("DELETE", "/api/v3/order", params, self._headers(), retry=False)


def _parse_order(data: dict[str, Any]) -> OrderResult:
    executed = Decimal(str(data.get("executedQty", "0")))
    quote_qty = Decimal(str(data.get("cummulativeQuoteQty", "0")))
    ts_ms = data.get("transactTime") or data.get("updateTime") or data.get("time")
    ts = from_ms(int(ts_ms)) if ts_ms else utc_now()
    fills = [
        Fill(
            price=Decimal(f["price"]),
            quantity=Decimal(f["qty"]),
            fee=Decimal(f.get("commission", "0")),
            fee_asset=f.get("commissionAsset", ""),
            ts=ts,
        )
        for f in data.get("fills", [])
    ]
    raw_status = str(data.get("status", "UNKNOWN"))
    known = {"FILLED", "PARTIALLY_FILLED", "NEW", "REJECTED", "CANCELED", "EXPIRED"}
    raw_type = str(data.get("type", "MARKET"))
    return OrderResult(
        client_order_id=str(data.get("clientOrderId", "")),
        exchange_order_id=str(data["orderId"]) if "orderId" in data else None,
        symbol=str(data.get("symbol", "")),
        side=Side(str(data.get("side", "BUY"))),
        order_type=OrderType.MARKET if raw_type == "MARKET" else OrderType.LIMIT,
        status=raw_status if raw_status in known else "UNKNOWN",  # type: ignore[arg-type]
        requested_qty=Decimal(str(data.get("origQty", "0"))),
        executed_qty=executed,
        avg_price=(quote_qty / executed) if executed > 0 else None,
        fills=fills,
        detail=f"exchange type={raw_type}",
    )

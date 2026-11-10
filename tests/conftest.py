"""Shared fixtures: synthetic candles, a fake market-data source and a desk wired to temp storage."""

from __future__ import annotations

import shutil
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trader.config import Settings, TradingMode
from trader.desk import TradingDesk
from trader.domain import Quote, SymbolFilters
from trader.timeutil import interval_ms, utc_now

REPO = Path(__file__).resolve().parents[1]


def synthetic_candles(
    n: int = 3000,
    interval: str = "1h",
    seed: int = 1,
    end_ms: int | None = None,
    start_price: float = 100.0,
    drift: float = 0.0,
) -> pd.DataFrame:
    """Deterministic random-walk candles whose last bar closed just before ``end_ms``."""
    rng = np.random.default_rng(seed)
    step = interval_ms(interval)
    end_ms = end_ms or int(time.time() * 1000)
    last_open = (end_ms // step) * step - step
    open_time = last_open - step * np.arange(n - 1, -1, -1)
    returns = rng.normal(drift, 0.006, n)
    close = start_price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[start_price], close[:-1]])
    wiggle = np.abs(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    volume = rng.uniform(50, 150, n)
    return pd.DataFrame(
        {
            "open_time": open_time.astype("int64"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": (open_time + step - 1).astype("int64"),
            "quote_volume": volume * close,
            "trades": np.full(n, 1000, dtype="int64"),
            "taker_buy_base": volume * rng.uniform(0.4, 0.6, n),
            "taker_buy_quote": volume * close * 0.5,
        }
    )


def frame_to_klines(frame: pd.DataFrame) -> list[list]:
    return [
        [int(r.open_time), f"{r.open:.8f}", f"{r.high:.8f}", f"{r.low:.8f}", f"{r.close:.8f}", f"{r.volume:.8f}", int(r.close_time),
         f"{r.quote_volume:.8f}", int(r.trades), f"{r.taker_buy_base:.8f}", f"{r.taker_buy_quote:.8f}", "0"]
        for r in frame.itertuples(index=False)
    ]  # fmt: skip


class FakeSource:
    """In-memory MarketDataSource. Tests mutate ``bid``/``ask``/``quote_age`` and ``override_klines``."""

    def __init__(self) -> None:
        self.bid = Decimal("100.00")
        self.ask = Decimal("100.01")
        self.quote_age_seconds = 0.0
        self.quote_volume = "900000000"
        self.fail_quote = False
        self.override_klines: dict[str, pd.DataFrame] = {}
        self.calls: list[str] = []
        # as reported by exchangeInfo for the universe symbols on 2026-11-10
        self.order_types = ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"]

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list]:
        self.calls.append(f"klines:{symbol}:{interval}")
        frame = self.override_klines.get(interval)
        if frame is None:
            frame = synthetic_candles(3000, interval, seed=sum(map(ord, symbol + interval)))
        if start_ms is not None:
            frame = frame[frame["open_time"] >= start_ms]
        if end_ms is not None:
            frame = frame[frame["open_time"] <= end_ms]
        return frame_to_klines(frame.head(limit))

    def quote(self, symbol: str) -> Quote:
        self.calls.append(f"quote:{symbol}")
        if self.fail_quote:
            raise ConnectionError("simulated outage")
        from datetime import timedelta

        return Quote(
            symbol=symbol,
            bid=self.bid,
            ask=self.ask,
            bid_qty=Decimal(5),
            ask_qty=Decimal(5),
            observed_at=utc_now() - timedelta(seconds=self.quote_age_seconds),
        )

    def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "priceChangePercent": "1.5",
            "highPrice": "105",
            "lowPrice": "95",
            "quoteVolume": self.quote_volume,
        }

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(
            symbol=symbol, status="TRADING", base_asset=symbol.removesuffix("USDT"), quote_asset="USDT", tick_size=Decimal("0.01"),
            min_price=Decimal("0.01"), max_price=Decimal("1000000"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
            max_qty=Decimal("9000"), min_notional=Decimal("5"), order_types=list(self.order_types), fetched_at=utc_now(),
        )  # fmt: skip

    def futures_premium_index(self, symbol: str) -> dict[str, Any]:
        return {
            "markPrice": "100.1",
            "indexPrice": "100.0",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1794326400000,
            "time": 1794300979000,
        }

    def futures_open_interest(self, symbol: str) -> dict[str, Any]:
        return {"openInterest": "1234.5", "time": 1794300972737}


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A minimal copy of the repo layout so tests never touch real configs, knowledge or data."""
    shutil.copytree(REPO / "configs", tmp_path / "configs")
    (tmp_path / "knowledge" / "source_registry").mkdir(parents=True)
    (tmp_path / "knowledge" / "source_registry" / "sources.yaml").write_text(
        "domains:\n  primary_official: [binance.com, sec.gov]\n  reputable_secondary: [reuters.com]\n  social_community: [reddit.com, x.com]\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


def make_desk(
    root: Path, source: FakeSource, mode: TradingMode = TradingMode.PAPER, **kwargs: Any
) -> TradingDesk:
    settings = Settings(
        trading_mode=mode,
        trader_data_dir=root / "data",
        **{k: v for k, v in kwargs.items() if k not in {"broker"}},
    )
    return TradingDesk(root=root, settings=settings, source=source, providers=[], broker=kwargs.get("broker"))


@pytest.fixture
def desk(repo_copy: Path, source: FakeSource) -> TradingDesk:
    return make_desk(repo_copy, source)


def decision_payload(
    desk: TradingDesk, symbol: str = "BTCUSDT", decision: str = "LONG_CANDIDATE"
) -> dict[str, Any]:
    snapshot = desk.market_snapshot(symbol)
    return {
        "as_of": utc_now().isoformat(),
        "symbol": symbol,
        "horizon": "24h",
        "decision": decision,
        "quantitative_evidence": [
            {
                "kind": "observed_fact",
                "statement": "snapshot taken for the test",
                "evidence_ids": [snapshot["evidence_id"]],
            }
        ],
        "counter_evidence": ["test counter evidence"],
        "uncertainties": ["test uncertainty"],
        "invalidation_conditions": ["close below the stop"],
        "stop_plan": "2% below entry",
        "action_requested": "REQUEST_RISK_VALIDATION",
    }


def open_long(
    desk: TradingDesk,
    symbol: str = "BTCUSDT",
    stop: str = "98.00",
    key: str = "test-key-0001",
    take_profit: str | None = None,
) -> dict[str, Any]:
    decision = desk.record_decision_report(decision_payload(desk, symbol))
    assert decision["status"] == "ok", decision
    return desk.create_trade_intent(
        decision_id=decision["data"]["decision_id"], symbol=symbol, side="BUY", stop_price=stop, take_profit=take_profit,
        rationale="integration test long entry with a two percent stop", idempotency_key=key,
    )  # fmt: skip

"""Optional kline WebSocket collector.

The desk does not depend on it: interactive sessions refresh candles over REST. Run it only if you want
warm data:  ``python -m trader collect``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import pandas as pd

from trader.storage.candles import CANDLE_COLUMNS, CandleStore, klines_to_frame

log = logging.getLogger(__name__)


def stream_url(base_ws: str, symbols: list[str], interval: str) -> str:
    streams = "/".join(f"{s.lower()}@kline_{interval}" for s in symbols)
    return f"{base_ws}/stream?streams={streams}"


def parse_kline_message(raw: str | bytes) -> dict[str, Any] | None:
    """Return a candle row dict for a *closed* kline, else None. Malformed messages return None."""
    try:
        message = json.loads(raw)
        data = message.get("data", message)
        if data.get("e") != "kline":
            return None
        k = data["k"]
        if not k.get("x"):
            return None
        return {
            "symbol": str(k["s"]),
            "interval": str(k["i"]),
            "open_time": int(k["t"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "close_time": int(k["T"]),
            "quote_volume": float(k["q"]),
            "trades": int(k["n"]),
            "taker_buy_base": float(k["V"]),
            "taker_buy_quote": float(k["Q"]),
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class KlineCollector:
    """Persist closed klines; reconnect with capped exponential backoff; back-fill gaps via REST."""

    def __init__(
        self,
        url: str,
        store: CandleStore,
        backfill: Callable[[str, str], Any] | None = None,
        max_backoff: float = 60.0,
    ) -> None:
        self.url = url
        self.store = store
        self.backfill = backfill
        self.max_backoff = max_backoff
        self._stop = asyncio.Event()
        self.messages = 0
        self.stored = 0
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    def handle(self, raw: str | bytes) -> bool:
        self.messages += 1
        row = parse_kline_message(raw)
        if row is None:
            return False
        frame = pd.DataFrame([{c: row[c] for c in CANDLE_COLUMNS}])
        frame = klines_to_frame(frame[CANDLE_COLUMNS].values.tolist())
        self.store.upsert(row["symbol"], row["interval"], frame)
        self.stored += 1
        return True

    async def run(self, connect: Callable[[str], Any] | None = None) -> None:
        if connect is None:
            import websockets

            connect = websockets.connect
        attempt = 0
        while not self._stop.is_set():
            try:
                async with connect(self.url) as ws:
                    attempt = 0
                    if self.backfill is not None and self.reconnects > 0:
                        # fill whatever was missed while disconnected
                        await asyncio.to_thread(self.backfill, "*", "*")
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except TimeoutError:
                            continue
                        self.handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors of any kind -> reconnect
                if self._stop.is_set():
                    break
                self.reconnects += 1
                delay = min(self.max_backoff, 1.0 * 2**attempt) + random.uniform(0, 0.5)
                attempt += 1
                log.warning(
                    "kline stream disconnected (%s); reconnecting in %.1fs", type(exc).__name__, delay
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

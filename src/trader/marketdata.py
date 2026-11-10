"""Market-data service: REST backfill/refresh into the candle store, with quality reporting."""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd

from trader.domain import Quote, SymbolFilters
from trader.storage.candles import CandleStore, QualityReport, check_quality, drop_unclosed, klines_to_frame
from trader.timeutil import interval_ms, to_ms, utc_now


class MarketDataSource(Protocol):
    """What the desk needs from an exchange for reading. Implemented by BinancePublicClient and test fakes."""

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list]: ...

    def quote(self, symbol: str) -> Quote: ...

    def ticker_24h(self, symbol: str) -> dict[str, Any]: ...

    def symbol_filters(self, symbol: str) -> SymbolFilters: ...

    def futures_premium_index(self, symbol: str) -> dict[str, Any]: ...

    def futures_open_interest(self, symbol: str) -> dict[str, Any]: ...


class MarketData:
    def __init__(self, source: MarketDataSource, store: CandleStore) -> None:
        self.source = source
        self.store = store
        self._filters: dict[str, SymbolFilters] = {}

    def backfill(self, symbol: str, interval: str, days: int, now_ms: int | None = None) -> pd.DataFrame:
        """Download ``days`` of history (paged, 1000 bars per request) and merge into the store."""
        now_ms = now_ms or to_ms(utc_now())
        step = interval_ms(interval)
        start = now_ms - days * 86_400_000
        last_stored = self.store.last_open_time(symbol, interval)
        first_stored: int | None = None
        if last_stored is not None:
            first_stored = int(self.store.load(symbol, interval)["open_time"].min())
        # only fetch what is missing at the tail if the stored history already covers the start
        cursor = (
            start if first_stored is None or first_stored > start + step else (last_stored or start) + step
        )
        frames: list[pd.DataFrame] = []
        while cursor < now_ms:
            rows = self.source.klines(symbol, interval, start_ms=cursor, limit=1000)
            if not rows:
                break
            frame = drop_unclosed(klines_to_frame(rows), now_ms)
            if frame.empty:
                break
            frames.append(frame)
            next_cursor = int(frame["open_time"].iloc[-1]) + step
            if next_cursor <= cursor or len(rows) < 1000:
                break
            cursor = next_cursor
        new_rows = pd.concat(frames, ignore_index=True) if frames else klines_to_frame([])
        return self.store.upsert(symbol, interval, new_rows)

    def refresh(
        self, symbol: str, interval: str, min_bars: int = 500, now_ms: int | None = None
    ) -> pd.DataFrame:
        """Bring the store up to the latest closed bar; fetch ``min_bars`` if the store is empty/short."""
        now_ms = now_ms or to_ms(utc_now())
        step = interval_ms(interval)
        stored = self.store.load(symbol, interval)
        if len(stored) < min_bars:
            days = max(1, int(min_bars * step / 86_400_000) + 2)
            return self.backfill(symbol, interval, days, now_ms)
        last = int(stored["open_time"].max())
        if now_ms - (last + step) < step:  # next bar has not closed yet
            return stored
        frames: list[pd.DataFrame] = []
        cursor = last + step
        while cursor < now_ms:
            rows = self.source.klines(symbol, interval, start_ms=cursor, limit=1000)
            frame = drop_unclosed(klines_to_frame(rows), now_ms)
            if frame.empty:
                break
            frames.append(frame)
            if len(rows) < 1000:
                break
            cursor = int(frame["open_time"].iloc[-1]) + step
        if not frames:
            return stored
        return self.store.upsert(symbol, interval, pd.concat(frames, ignore_index=True))

    def candles(
        self,
        symbol: str,
        interval: str,
        *,
        refresh: bool = True,
        min_bars: int = 500,
        max_staleness_bars: float = 2,
        window_bars: int | None = None,
    ) -> tuple[pd.DataFrame, QualityReport]:
        """Latest candles plus a quality report over the ``window_bars`` most recent bars (default ``min_bars``:
        the history the features need). Call ``check_quality`` directly to audit the whole stored history."""
        now_ms = to_ms(utc_now())
        frame = (
            self.refresh(symbol, interval, min_bars, now_ms) if refresh else self.store.load(symbol, interval)
        )
        window = window_bars if window_bars is not None else min_bars
        return frame, check_quality(frame, symbol, interval, now_ms, max_staleness_bars, window_bars=window)

    def filters(self, symbol: str, max_age_seconds: float = 3600) -> SymbolFilters:
        cached = self._filters.get(symbol)
        if cached is not None and (utc_now() - cached.fetched_at).total_seconds() <= max_age_seconds:
            return cached
        fresh = self.source.symbol_filters(symbol)
        self._filters[symbol] = fresh
        return fresh

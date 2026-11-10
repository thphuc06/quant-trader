"""Parquet candle store and data-quality checks.

Candles are research data: prices are float64 here. Anything that becomes an order is re-read from the
exchange as Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from trader.timeutil import interval_ms

CANDLE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
]
_FLOAT_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base",
    "taker_buy_quote",
]
_INT_COLUMNS = ["open_time", "close_time", "trades"]


def klines_to_frame(rows: list[list]) -> pd.DataFrame:
    """Convert Binance kline arrays (12 fields, see research/binance-api-research.md) to a typed frame."""
    if not rows:
        return empty_frame()
    frame = pd.DataFrame([row[:11] for row in rows], columns=CANDLE_COLUMNS)
    for column in _FLOAT_COLUMNS:
        frame[column] = frame[column].astype("float64")
    for column in _INT_COLUMNS:
        frame[column] = frame[column].astype("int64")
    return frame


def empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype="float64") for column in CANDLE_COLUMNS})
    for column in _INT_COLUMNS:
        frame[column] = frame[column].astype("int64")
    return frame


def drop_unclosed(frame: pd.DataFrame, now_ms: int) -> pd.DataFrame:
    """Remove candles that have not closed yet (close_time is the last millisecond of the bar)."""
    return frame[frame["close_time"] < now_ms].reset_index(drop=True)


@dataclass
class QualityReport:
    symbol: str
    interval: str
    rows: int
    first_open_time: int | None
    last_open_time: int | None
    gaps: int
    missing_bars: int
    duplicates: int
    out_of_order: bool
    invalid_rows: int
    staleness_bars: float | None
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "rows": self.rows,
            "first_open_time_ms": self.first_open_time,
            "last_open_time_ms": self.last_open_time,
            "gaps": self.gaps,
            "missing_bars": self.missing_bars,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "invalid_rows": self.invalid_rows,
            "staleness_bars": self.staleness_bars,
            "status": "ok" if self.ok else "degraded",
            "issues": self.issues,
        }


def check_quality(
    frame: pd.DataFrame, symbol: str, interval: str, now_ms: int | None = None, max_staleness_bars: float = 2
) -> QualityReport:
    """Detect gaps, duplicates, disorder, impossible OHLC rows and staleness. Never repairs silently."""
    step = interval_ms(interval)
    issues: list[str] = []
    if frame.empty:
        return QualityReport(symbol, interval, 0, None, None, 0, 0, 0, False, 0, None, ["no data"])

    times = frame["open_time"]
    duplicates = int(times.duplicated().sum())
    out_of_order = not bool(times.is_monotonic_increasing)
    diffs = times.sort_values().drop_duplicates().diff().dropna()
    gap_mask = diffs > step
    gaps = int(gap_mask.sum())
    missing = int(((diffs[gap_mask] // step) - 1).sum()) if gaps else 0
    invalid = int(
        (
            (frame["high"] < frame["low"])
            | (frame["high"] < frame[["open", "close"]].max(axis=1))
            | (frame["low"] > frame[["open", "close"]].min(axis=1))
            | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (frame["volume"] < 0)
        ).sum()
    )
    staleness: float | None = None
    if now_ms is not None:
        last_close = int(frame["close_time"].max())
        staleness = max(0.0, (now_ms - last_close) / step)
        if staleness > max_staleness_bars:
            issues.append(f"stale: last closed bar is {staleness:.1f} bars old (limit {max_staleness_bars})")
    if duplicates:
        issues.append(f"{duplicates} duplicate open_time values")
    if out_of_order:
        issues.append("rows out of order")
    if gaps:
        issues.append(f"{gaps} gaps, {missing} missing bars")
    if invalid:
        issues.append(f"{invalid} rows with impossible OHLCV values")
    return QualityReport(
        symbol,
        interval,
        len(frame),
        int(times.min()),
        int(times.max()),
        gaps,
        missing,
        duplicates,
        out_of_order,
        invalid,
        staleness,
        issues,
    )


class CandleStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def _path(self, symbol: str, interval: str) -> Path:
        if not symbol.isalnum() or not interval.isalnum():
            raise ValueError("symbol and interval must be alphanumeric")
        return self.base_dir / symbol.upper() / f"{interval}.parquet"

    def load(self, symbol: str, interval: str) -> pd.DataFrame:
        path = self._path(symbol, interval)
        if not path.exists():
            return empty_frame()
        return pd.read_parquet(path)

    def upsert(self, symbol: str, interval: str, new_rows: pd.DataFrame) -> pd.DataFrame:
        """Merge new candles; the newest copy of an open_time wins; result sorted and de-duplicated."""
        existing = self.load(symbol, interval)
        frames = [f for f in (existing, new_rows) if not f.empty]
        if not frames:
            return empty_frame()
        merged = (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset="open_time", keep="last")
            .sort_values("open_time")
            .reset_index(drop=True)
        )
        path = self._path(symbol, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(path)
        return merged

    def last_open_time(self, symbol: str, interval: str) -> int | None:
        frame = self.load(symbol, interval)
        return None if frame.empty else int(frame["open_time"].max())

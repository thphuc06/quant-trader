"""Collect perpetual-futures funding rates and open interest into append-only tables.

Why now: Binance serves open-interest history for only about 30 days (verified 2026-11-11), so every day this does not
run is lost for good. Funding history is served back to each contract's listing, so a first run backfills it.

Data are context for research, not signals: nothing here has been tested for predictive value. Values are stored as
the exchange's raw decimal strings; missing snapshots are reported, never filled.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from trader.storage.db import Database
from trader.timeutil import from_ms, iso, to_ms, utc_now

FUNDING_EPOCH_MS = (
    1_567_296_000_000  # 2019-09-01: before the first USDT-M perpetual; the API starts at listing
)
OI_PERIOD = "1h"
OI_PERIOD_MS = 3_600_000
OI_RETENTION_DAYS = 29  # request strictly inside the ~30-day window Binance serves
DAY_MS = 86_400_000
MAX_FUNDING_PAGES = 50  # 50 x 1000 events ~ 45 years of 8-hourly funding: a guard against a runaway loop


class DerivativesSource(Protocol):
    def futures_funding_history(
        self, symbol: str, start_ms: int, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

    def futures_open_interest_history(
        self, symbol: str, period: str, start_ms: int, end_ms: int, limit: int = 500
    ) -> list[dict[str, Any]]: ...


def _decimal_text(value: Any) -> str:
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def _last(db: Database, sql: str, params: tuple[Any, ...]) -> int | None:
    value = db.query(sql, params)[0]["last"]
    return int(value) if value is not None else None


def collect_funding(db: Database, source: DerivativesSource, symbol: str, now_ms: int) -> dict[str, Any]:
    logged_at = iso(from_ms(now_ms))
    last = _last(db, "SELECT MAX(funding_time_ms) AS last FROM funding_rates WHERE symbol=?", (symbol,))
    cursor = last + 1 if last is not None else FUNDING_EPOCH_MS
    inserted = 0
    for _ in range(MAX_FUNDING_PAGES):
        rows = [
            r for r in source.futures_funding_history(symbol, cursor, 1000) if int(r["fundingTime"]) <= now_ms
        ]
        if not rows:
            break
        with db.transaction() as conn:
            for row in rows:
                cur = conn.execute(
                    "INSERT INTO funding_rates(symbol, funding_time_ms, funding_rate, mark_price, logged_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        symbol,
                        int(row["fundingTime"]),
                        _decimal_text(row["fundingRate"]),
                        _decimal_text(row["markPrice"]) if row.get("markPrice") not in (None, "") else None,
                        logged_at,
                    ),
                )
                inserted += max(cur.rowcount, 0)
        cursor = max(int(r["fundingTime"]) for r in rows) + 1
        if len(rows) < 1000:
            break
    total = db.query(
        "SELECT COUNT(*) AS n, MIN(funding_time_ms) AS first, MAX(funding_time_ms) AS last FROM funding_rates "
        "WHERE symbol=?",
        (symbol,),
    )[0]
    return {
        "inserted": inserted,
        "stored": int(total["n"]),
        "first": iso(from_ms(int(total["first"]))) if total["first"] is not None else None,
        "last": iso(from_ms(int(total["last"]))) if total["last"] is not None else None,
    }


def collect_open_interest(
    db: Database, source: DerivativesSource, symbol: str, now_ms: int
) -> dict[str, Any]:
    logged_at = iso(from_ms(now_ms))
    floor = now_ms - OI_RETENTION_DAYS * DAY_MS
    last = _last(
        db,
        "SELECT MAX(timestamp_ms) AS last FROM open_interest WHERE symbol=? AND period=?",
        (symbol, OI_PERIOD),
    )
    start = floor if last is None else max(floor, last + 1)
    lost_hours = 0  # snapshots that aged out of the API's window since the last run: gone for good
    if last is not None:
        first_fetchable = -(-floor // OI_PERIOD_MS) * OI_PERIOD_MS  # ceil to the period
        lost_hours = max(0, (first_fetchable - last) // OI_PERIOD_MS - 1)
    inserted = 0
    cursor = start
    while cursor < now_ms:
        # one day (inclusive ends, <= 25 rows) per request: wider windows return only their LAST rows
        end = min(cursor + DAY_MS, now_ms)
        rows = source.futures_open_interest_history(symbol, OI_PERIOD, cursor, end, 500)
        with db.transaction() as conn:
            for row in rows:
                timestamp = int(row["timestamp"])
                if not cursor <= timestamp <= now_ms:
                    continue
                cur = conn.execute(
                    "INSERT INTO open_interest(symbol, period, timestamp_ms, sum_open_interest, "
                    "sum_open_interest_value, logged_at) VALUES (?,?,?,?,?,?)",
                    (
                        symbol,
                        OI_PERIOD,
                        timestamp,
                        _decimal_text(row["sumOpenInterest"]),
                        _decimal_text(row["sumOpenInterestValue"]),
                        logged_at,
                    ),
                )
                inserted += max(cur.rowcount, 0)
        cursor = end + 1
    window = db.query(
        "SELECT COUNT(*) AS n, MIN(timestamp_ms) AS first, MAX(timestamp_ms) AS last FROM open_interest "
        "WHERE symbol=? AND period=?",
        (symbol, OI_PERIOD),
    )[0]
    first, newest = window["first"], window["last"]
    expected = (int(newest) - int(first)) // OI_PERIOD_MS + 1 if first is not None else 0
    return {
        "inserted": inserted,
        "stored": int(window["n"]),
        "first": iso(from_ms(int(first))) if first is not None else None,
        "last": iso(from_ms(int(newest))) if newest is not None else None,
        "missing_snapshots": max(0, expected - int(window["n"])),
        "hours_lost_beyond_retention": int(lost_hours),
    }


def collect_derivatives(
    db: Database, source: DerivativesSource, symbols: list[str], now_ms: int | None = None
) -> dict[str, Any]:
    """Funding and open interest for every symbol; one failing symbol does not stop the others."""
    now_ms = now_ms if now_ms is not None else to_ms(utc_now())
    out: dict[str, Any] = {"collected_at": iso(from_ms(now_ms)), "symbols": {}}
    for symbol in symbols:
        entry: dict[str, Any] = {}
        for name, fn in (("funding", collect_funding), ("open_interest", collect_open_interest)):
            try:
                entry[name] = {"status": "ok", **fn(db, source, symbol, now_ms)}
            except Exception as exc:
                entry[name] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        out["symbols"][symbol] = entry
    out["ok"] = all(part["status"] == "ok" for entry in out["symbols"].values() for part in entry.values())
    return out

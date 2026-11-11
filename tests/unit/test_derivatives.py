"""Funding / open-interest collector: paging, idempotency, append-only storage, lost-history reporting."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from trader.derivatives import DAY_MS, OI_PERIOD_MS, OI_RETENTION_DAYS, collect_derivatives
from trader.storage.db import Database

NOW = 1_794_376_800_000  # an exact hour
EIGHT_H = 8 * 3_600_000


class FakeFutures:
    """Mimics the verified API behaviour: funding ascending from start_ms; OI only ~30 days back and the LAST rows of
    a wide window."""

    def __init__(self, funding_from: int, funding_count: int) -> None:
        self.funding = [funding_from + i * EIGHT_H for i in range(funding_count)]
        self.oi_calls: list[tuple[int, int]] = []
        self.fail_oi = False

    def futures_funding_history(self, symbol: str, start_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        rows = [t for t in self.funding if t >= start_ms][:limit]
        return [
            {"symbol": symbol, "fundingTime": t, "fundingRate": "0.00010000", "markPrice": "100.5"}
            for t in rows
        ]

    def futures_open_interest_history(self, symbol, period, start_ms, end_ms, limit=500):  # type: ignore[no-untyped-def]
        if self.fail_oi:
            raise ConnectionError("simulated outage")
        if start_ms < NOW - 30 * DAY_MS:
            raise ValueError("-1130 parameter 'startTime' is invalid.")
        self.oi_calls.append((start_ms, end_ms))
        first = -(-start_ms // OI_PERIOD_MS) * OI_PERIOD_MS
        stamps = [t for t in range(first, end_ms + 1, OI_PERIOD_MS)][-limit:]
        return [{"symbol": symbol, "timestamp": t, "sumOpenInterest": "100.5", "sumOpenInterestValue": "7.3e9"}
                for t in stamps]  # fmt: skip


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.sqlite")
    database.init_schema()
    return database


def test_first_run_backfills_all_funding_pages_and_29_days_of_open_interest(tmp_path: Path) -> None:
    store = db(tmp_path)
    source = FakeFutures(NOW - 2500 * EIGHT_H, 2501)  # spans three funding pages
    result = collect_derivatives(store, source, ["BTCUSDT"], now_ms=NOW)
    btc = result["symbols"]["BTCUSDT"]
    assert result["ok"] and btc["funding"]["inserted"] == 2501
    oi = btc["open_interest"]
    assert oi["inserted"] == OI_RETENTION_DAYS * 24 + 1 and oi["missing_snapshots"] == 0
    assert all(end - start <= DAY_MS for start, end in source.oi_calls)  # paged by day, never a wide window


def test_rerun_is_idempotent_and_storage_is_append_only(tmp_path: Path) -> None:
    store = db(tmp_path)
    source = FakeFutures(NOW - 30 * EIGHT_H, 31)
    collect_derivatives(store, source, ["ETHUSDT"], now_ms=NOW)
    again = collect_derivatives(store, source, ["ETHUSDT"], now_ms=NOW)
    assert again["symbols"]["ETHUSDT"]["funding"]["inserted"] == 0
    assert again["symbols"]["ETHUSDT"]["open_interest"]["inserted"] == 0
    for table in ("funding_rates", "open_interest"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.execute(f"UPDATE {table} SET logged_at='x'")


def test_a_gap_longer_than_retention_is_reported_as_lost_not_filled(tmp_path: Path) -> None:
    store = db(tmp_path)
    source = FakeFutures(NOW - 10 * EIGHT_H, 11)
    early = NOW - 40 * DAY_MS
    store.execute(
        "INSERT INTO open_interest VALUES ('BTCUSDT','1h',?, '1', '1', 'then')", (early,)
    )  # last run 40 days ago
    oi = collect_derivatives(store, source, ["BTCUSDT"], now_ms=NOW)["symbols"]["BTCUSDT"]["open_interest"]
    assert oi["hours_lost_beyond_retention"] == (NOW - OI_RETENTION_DAYS * DAY_MS - early) // OI_PERIOD_MS - 1
    assert oi["missing_snapshots"] == oi["hours_lost_beyond_retention"]


def test_one_failing_part_is_reported_and_the_rest_is_still_collected(tmp_path: Path) -> None:
    store = db(tmp_path)
    source = FakeFutures(NOW - 3 * EIGHT_H, 4)
    source.fail_oi = True
    result = collect_derivatives(store, source, ["BTCUSDT", "SOLUSDT"], now_ms=NOW)
    assert not result["ok"]
    assert result["symbols"]["SOLUSDT"]["funding"]["status"] == "ok"
    assert result["symbols"]["SOLUSDT"]["open_interest"]["status"] == "error"

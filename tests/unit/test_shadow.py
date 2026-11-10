"""Forward shadow log: idempotency, causality, missing bars, append-only storage, read tool."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import FakeSource, make_desk, synthetic_candles
from trader.shadow import append_rows, build_shadow_rows, last_logged_open_ms, read_rows, run_shadow_log
from trader.storage.db import Database
from trader.timeutil import interval_ms

STEP = interval_ms("1h")
END_MS = 1_794_000_000_000 - (1_794_000_000_000 % STEP)  # fixed clock: tests never depend on wall time


def _build(candles, last_logged, now_ms, **overrides):
    kwargs = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "last_logged_open_ms": last_logged,
        "feature_params": {},
        "strategy_params": {},
        "validation": {},
        "quality_window_bars": 500,
        "commit": "abc1234",
        "dirty": False,
        "now_ms": now_ms,
    }
    kwargs.update(overrides)
    return build_shadow_rows(candles, **kwargs)


def _comparable(row: dict) -> dict:
    return {
        k: row[k]
        for k in ("open_time_ms", "bar_status", "close", "features", "regime", "strategies", "data_quality")
    }


def test_row_for_bar_t_uses_only_data_closed_at_t() -> None:
    candles = synthetic_candles(1200, end_ms=END_MS, seed=3)
    now = int(candles["close_time"].iloc[-1]) + 1
    first = len(candles) - 12
    full = _build(candles, int(candles["open_time"].iloc[first - 1]), now)
    assert len(full) == 12
    for k, row in enumerate(full):
        t = first + k
        truncated = _build(candles.iloc[: t + 1], int(candles["open_time"].iloc[t - 1]), now)
        assert len(truncated) == 1
        assert _comparable(truncated[0]) == _comparable(row)
    # changing the future must not change the past
    shocked = candles.copy()
    shocked.loc[first + 6 :, ["open", "high", "low", "close"]] *= 3.0
    shocked_rows = _build(shocked, int(candles["open_time"].iloc[first - 1]), now)
    assert [_comparable(r) for r in shocked_rows[:6]] == [_comparable(r) for r in full[:6]]
    assert _comparable(shocked_rows[6]) != _comparable(full[6])


def test_unclosed_bars_are_never_logged() -> None:
    candles = synthetic_candles(800, end_ms=END_MS, seed=4)
    last_close = int(candles["close_time"].iloc[-1])
    rows = _build(candles, int(candles["open_time"].iloc[-4]), now_ms=last_close)  # last bar not closed yet
    assert [r["open_time_ms"] for r in rows] == [int(t) for t in candles["open_time"].iloc[-3:-1]]


def test_first_run_logs_only_the_latest_closed_bar() -> None:
    candles = synthetic_candles(800, end_ms=END_MS, seed=5)
    rows = _build(candles, None, int(candles["close_time"].iloc[-1]) + 1)
    assert len(rows) == 1 and rows[0]["open_time_ms"] == int(candles["open_time"].iloc[-1])
    row = rows[0]
    assert (
        row["bar_status"] == "closed" and row["code_commit"] == "abc1234" and row["logged_at"].endswith("Z")
    )
    assert set(row["strategies"]) == {"tsmom_trend_v1", "donchian_breakout_v1"}
    assert all(s["validation_status"] == "not_validated" for s in row["strategies"].values())
    assert row["regime"]["method"] == "rule_regime_v1" and "rsi" in row["features"]


def test_missing_bar_is_recorded_as_missing_not_filled() -> None:
    candles = synthetic_candles(800, end_ms=END_MS, seed=6)
    hole = len(candles) - 3
    hole_time = int(candles["open_time"].iloc[hole])
    with_gap = candles.drop(index=hole).reset_index(drop=True)
    rows = _build(with_gap, int(candles["open_time"].iloc[-6]), int(candles["close_time"].iloc[-1]) + 1)
    assert [r["open_time_ms"] for r in rows] == [int(t) for t in candles["open_time"].iloc[-5:]]
    missing = [r for r in rows if r["bar_status"] == "missing"]
    assert len(missing) == 1 and missing[0]["open_time_ms"] == hole_time
    assert missing[0]["close"] is None and missing[0]["features"] is None and missing[0]["strategies"] is None
    after = [r for r in rows if r["open_time_ms"] > hole_time]
    assert all(r["data_quality"]["status"] == "degraded" for r in after)  # the gap sits inside their window


def test_storage_is_append_only_and_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    db.init_schema()
    candles = synthetic_candles(800, end_ms=END_MS, seed=7)
    rows = _build(candles, int(candles["open_time"].iloc[-4]), int(candles["close_time"].iloc[-1]) + 1)
    assert append_rows(db, rows) == 3
    assert append_rows(db, rows) == 0
    altered = [{**rows[0], "close": 1.0, "code_commit": "evil"}]
    assert append_rows(db, altered) == 0
    assert read_rows(db, "BTCUSDT", "1h", 10)[0]["close"] == rows[0]["close"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE shadow_log SET bar_status='missing'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM shadow_log")
    db.execute(
        "INSERT OR REPLACE INTO shadow_log VALUES (?,?,?,?,?,?,?,?)",
        ("BTCUSDT", "1h", rows[0]["open_time_ms"], rows[0]["close_time_ms"], "missing", "x", "evil", "{}"),
    )
    assert read_rows(db, "BTCUSDT", "1h", 10)[0]["close"] == rows[0]["close"]
    assert len(read_rows(db, "BTCUSDT", "1h", 10)) == 3


def test_run_appends_new_bars_once_and_tool_reads_them(repo_copy: Path) -> None:
    source = FakeSource()
    full = synthetic_candles(3000, end_ms=END_MS, seed=8)
    hole_index = 2995
    source.override_klines["1h"] = full.drop(index=hole_index).reset_index(drop=True)
    desk = make_desk(repo_copy, source)

    first_now = int(full["close_time"].iloc[2990]) + 60_000  # bar 2990 closed one minute ago
    first = run_shadow_log(desk, ["BTCUSDT"], now_ms=first_now)
    assert first.ok and first.symbols["BTCUSDT"]["rows_inserted"] == 1
    assert last_logged_open_ms(desk.db, "BTCUSDT", "1h") == int(full["open_time"].iloc[2990])

    second_now = int(full["close_time"].iloc[2999]) + 60_000
    second = run_shadow_log(desk, ["BTCUSDT"], now_ms=second_now)
    assert second.symbols["BTCUSDT"]["rows_inserted"] == 9
    assert second.symbols["BTCUSDT"]["missing_bars_inserted"] == 1
    again = run_shadow_log(desk, ["BTCUSDT"], now_ms=second_now + 5 * 60_000)
    assert again.symbols["BTCUSDT"]["rows_inserted"] == 0

    envelope = desk.shadow_log("btcusdt", limit=500)
    assert envelope["status"] in {"ok", "stale"}  # fixed test clock is in the past relative to wall time
    data = envelope["data"]
    assert data["summary"]["rows"] == 10 and data["summary"]["missing_bars"] == 1
    assert [r["open_time_ms"] for r in data["rows"]] == [int(t) for t in full["open_time"].iloc[2990:3000]]
    assert any("missing" in w for w in envelope["warnings"])
    assert envelope["evidence_id"].startswith("ev")


def test_shadow_tool_is_unavailable_when_empty_and_rejects_unknown_symbols(repo_copy: Path) -> None:
    desk = make_desk(repo_copy, FakeSource())
    assert desk.shadow_log("ETHUSDT")["status"] == "unavailable"
    with pytest.raises(ValueError):
        desk.shadow_log("DOGEUSDT")


def test_a_failing_symbol_writes_nothing_and_reports_error(repo_copy: Path) -> None:
    class Broken(FakeSource):
        def klines(self, symbol, interval, **kwargs):  # type: ignore[no-untyped-def]
            if symbol == "SOLUSDT":
                raise ConnectionError("simulated outage")
            return super().klines(symbol, interval, **kwargs)

    desk = make_desk(repo_copy, Broken())
    result = run_shadow_log(desk, ["SOLUSDT", "BNBUSDT"], now_ms=END_MS + 60_000)
    assert not result.ok
    assert result.symbols["SOLUSDT"]["status"] == "error"
    assert result.symbols["BNBUSDT"]["status"] == "ok"
    assert last_logged_open_ms(desk.db, "SOLUSDT", "1h") is None

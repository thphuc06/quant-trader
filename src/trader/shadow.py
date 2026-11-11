"""Forward shadow log: what the mechanical system saw and wanted at every closed decision bar.

``python -m trader shadow-log`` (scheduled hourly by the operator) appends one row per closed bar not yet logged:
the bar, all registered features, the rule regime, each active strategy's target and validation status, the
data-quality status of the feature window, the code commit and ``logged_at``. No LLM, no orders, no repairs.

Rules
- Causal: a row for bar t is computed only from candles that closed at or before t. Features and strategy
  targets are trailing/forward-only functions, so computing them once on the stored history and reading row t is
  identical to recomputing on history truncated at t; ``tests/unit/test_shadow.py`` checks that equality.
- Append-only and idempotent: the ``shadow_log`` table ignores a second insert for the same bar and rejects
  UPDATE/DELETE (``storage/db.py``).
- Missing bars (an expected open time with no candle between two candles we do have) are written as
  ``bar_status = 'missing'`` with no values. Nothing is interpolated.
- The first run for a symbol logs only the latest closed bar: this log is forward data, not a backfill.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

from trader.quant.features import FEATURE_NAMES, classify_regime, compute_features
from trader.quant.research import ExperimentRegistry, code_commit, working_tree_dirty
from trader.quant.strategies import ACTIVE_STRATEGIES, STRATEGIES
from trader.storage.candles import check_quality
from trader.storage.db import Database, canonical_json
from trader.timeutil import from_ms, interval_ms, iso, to_ms, utc_now

if TYPE_CHECKING:
    from trader.desk import TradingDesk

SHADOW_SCHEMA = "shadow_log_v1"
MAX_READ_LIMIT = 200


def _num(value: Any) -> float | None:
    """Finite float or None. Never invents a value for NaN/inf."""
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def latest_validation(registry: ExperimentRegistry, symbol: str, interval: str) -> dict[str, dict[str, Any]]:
    """Validation status per strategy from the most recent single-strategy backtest record (default not_validated)."""
    latest: dict[str, dict[str, Any]] = {}
    for record in registry.records("backtest"):
        if record.get("symbol") == symbol and record.get("interval") == interval:
            latest[record["strategy"]] = record
    out: dict[str, dict[str, Any]] = {}
    for name in ACTIVE_STRATEGIES:
        chosen = latest.get(name)
        out[name] = {
            "status": "passed_provisional_gates"
            if chosen and chosen.get("all_gates_passed")
            else "not_validated",
            "experiment_id": chosen.get("experiment_id") if chosen else None,
        }
    return out


def build_shadow_rows(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    last_logged_open_ms: int | None,
    feature_params: dict[str, int],
    strategy_params: dict[str, dict[str, float]],
    validation: dict[str, dict[str, Any]],
    quality_window_bars: int,
    commit: str,
    dirty: bool | None,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Rows for every bar after ``last_logged_open_ms`` up to the latest closed candle.

    ``candles`` must hold closed bars only; anything whose close time is not before ``now_ms`` is dropped here
    as a second guard.
    """
    step = interval_ms(interval)
    frame = candles[candles["close_time"] < now_ms]
    frame = frame.drop_duplicates("open_time", keep="first").sort_values("open_time").reset_index(drop=True)
    if frame.empty:
        return []
    last_open = int(frame["open_time"].iloc[-1])
    if last_logged_open_ms is None:
        expected = [last_open]
    else:
        expected = list(range(last_logged_open_ms + step, last_open + 1, step))
    if not expected:
        return []

    features = compute_features(frame, interval, feature_params)
    targets = {
        name: STRATEGIES[name](frame, strategy_params.get(name)).target.to_numpy(dtype=float)
        for name in ACTIVE_STRATEGIES
    }
    position = {int(t): i for i, t in enumerate(frame["open_time"].to_numpy())}
    logged_at = iso(from_ms(now_ms))
    rows: list[dict[str, Any]] = []
    for open_ms in expected:
        close_ms = open_ms + step - 1
        base = {
            "schema": SHADOW_SCHEMA,
            "symbol": symbol,
            "interval": interval,
            "open_time": iso(from_ms(open_ms)),
            "open_time_ms": open_ms,
            "close_time": iso(from_ms(close_ms)),
            "close_time_ms": close_ms,
            "code_commit": commit,
            "working_tree_dirty": dirty,
            "logged_at": logged_at,
            "lag_seconds_after_close": round((now_ms - close_ms) / 1000.0, 3),
        }
        i = position.get(open_ms)
        if i is None:
            rows.append(
                {
                    **base,
                    "bar_status": "missing",
                    "close": None,
                    "features": None,
                    "regime": None,
                    "strategies": None,
                    "data_quality": {
                        "status": "missing",
                        "issues": [
                            "no candle from the exchange for this bar; recorded as missing, not filled"
                        ],
                    },
                }
            )
            continue
        row = features.iloc[i]
        quality = check_quality(
            frame.iloc[: i + 1], symbol, interval, now_ms=None, window_bars=quality_window_bars
        )
        rows.append(
            {
                **base,
                "bar_status": "closed",
                "close": _num(row["close"]),
                "features": {name: _num(row[name]) for name in FEATURE_NAMES},
                "regime": classify_regime(row),
                "strategies": {
                    name: {
                        "target": _num(targets[name][i]),
                        "target_position": "long" if targets[name][i] > 0 else "flat",
                        "validation_status": validation.get(name, {}).get("status", "not_validated"),
                        "validation_experiment_id": validation.get(name, {}).get("experiment_id"),
                    }
                    for name in ACTIVE_STRATEGIES
                },
                "data_quality": {
                    "status": quality.status,
                    "issues": list(quality.issues),
                    "checked_window_bars": quality.window_bars,
                    "note": "structural checks on the feature window ending at this bar; freshness is lag_seconds_after_close",
                },
            }
        )
    return rows


# ----------------------------------------------------------------------------- storage


def last_logged_open_ms(db: Database, symbol: str, interval: str) -> int | None:
    rows = db.query(
        "SELECT MAX(open_time_ms) AS last FROM shadow_log WHERE symbol=? AND interval=?", (symbol, interval)
    )
    value = rows[0]["last"] if rows else None
    return int(value) if value is not None else None


def append_rows(db: Database, rows: list[dict[str, Any]]) -> int:
    """Insert rows in one transaction; returns how many were new (re-inserts are ignored by a trigger)."""
    inserted = 0
    with db.transaction() as conn:
        for row in rows:
            cursor = conn.execute(
                "INSERT INTO shadow_log(symbol, interval, open_time_ms, close_time_ms, bar_status, logged_at, "
                "code_commit, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["symbol"],
                    row["interval"],
                    row["open_time_ms"],
                    row["close_time_ms"],
                    row["bar_status"],
                    row["logged_at"],
                    row["code_commit"],
                    canonical_json(row),
                ),
            )
            inserted += max(cursor.rowcount, 0)
    return inserted


def read_rows(db: Database, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    """The ``limit`` most recent rows, oldest first."""
    limit = max(1, min(int(limit), MAX_READ_LIMIT))
    rows = db.query(
        "SELECT payload_json FROM shadow_log WHERE symbol=? AND interval=? ORDER BY open_time_ms DESC LIMIT ?",
        (symbol, interval, limit),
    )
    return [json.loads(r["payload_json"]) for r in reversed(rows)]


def all_rows(db: Database, symbol: str, interval: str) -> list[dict[str, Any]]:
    """Every logged row for a symbol, oldest first (for evaluation code, not for the MCP tool)."""
    rows = db.query(
        "SELECT payload_json FROM shadow_log WHERE symbol=? AND interval=? ORDER BY open_time_ms",
        (symbol, interval),
    )
    return [json.loads(r["payload_json"]) for r in rows]


def summary(db: Database, symbol: str, interval: str) -> dict[str, Any]:
    rows = db.query(
        "SELECT COUNT(*) AS n, SUM(bar_status='missing') AS missing, MIN(open_time_ms) AS first, "
        "MAX(open_time_ms) AS last, MAX(logged_at) AS last_logged_at FROM shadow_log WHERE symbol=? AND interval=?",
        (symbol, interval),
    )
    r = rows[0]
    return {
        "rows": int(r["n"] or 0),
        "missing_bars": int(r["missing"] or 0),
        "first_bar_open": iso(from_ms(int(r["first"]))) if r["first"] is not None else None,
        "last_bar_open": iso(from_ms(int(r["last"]))) if r["last"] is not None else None,
        "last_logged_at": r["last_logged_at"],
    }


# ----------------------------------------------------------------------------- run


@dataclass
class ShadowRunResult:
    logged_at: str
    interval: str
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(item.get("status") == "ok" for item in self.symbols.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "logged_at": self.logged_at,
            "interval": self.interval,
            "ok": self.ok,
            "symbols": self.symbols,
        }


def run_shadow_log(
    desk: TradingDesk, symbols: list[str] | None = None, now_ms: int | None = None
) -> ShadowRunResult:
    """Refresh candles and append rows for every universe symbol on the decision timeframe."""
    now_ms = now_ms if now_ms is not None else to_ms(utc_now())
    interval = desk.app.decision_timeframe
    commit = code_commit(desk.root)
    dirty = working_tree_dirty(desk.root)
    result = ShadowRunResult(logged_at=iso(from_ms(now_ms)) or "", interval=interval)
    for symbol in symbols or desk.app.symbols:
        try:
            candles = desk.market.refresh(symbol, interval, desk.app.bars_needed(interval), now_ms)
            rows = build_shadow_rows(
                candles,
                symbol=symbol,
                interval=interval,
                last_logged_open_ms=last_logged_open_ms(desk.db, symbol, interval),
                feature_params=desk.app.feature_params(interval),
                strategy_params=desk.app.strategies,
                validation=latest_validation(desk.registry, symbol, interval),
                quality_window_bars=desk.app.bars_needed(interval),
                commit=commit,
                dirty=dirty,
                now_ms=now_ms,
            )
            inserted = append_rows(desk.db, rows)
            latest_close = int(candles["close_time"].max()) if not candles.empty else None
            behind = (now_ms - latest_close) / interval_ms(interval) if latest_close is not None else None
            result.symbols[symbol] = {
                "status": "ok",
                "rows_inserted": inserted,
                "missing_bars_inserted": sum(r["bar_status"] == "missing" for r in rows),
                "latest_closed_bar": iso(from_ms(latest_close - interval_ms(interval) + 1))
                if latest_close
                else None,
                "bars_since_latest_close": round(behind, 2) if behind is not None else None,
                "warning": (
                    "candle store is behind the clock; bars not yet fetched are not logged"
                    if behind is not None and behind > 2
                    else None
                ),
            }
        except Exception as exc:  # one symbol failing must not stop the others; nothing is written for it
            result.symbols[symbol] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    return result

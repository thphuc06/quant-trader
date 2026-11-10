"""SQLite operational store.

One short-lived connection per operation, WAL mode. Money columns are TEXT holding Decimal strings.
The ``journal`` table is append-only: triggers reject UPDATE and DELETE, and rows form a hash chain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from trader.domain import jsonable
from trader.timeutil import iso, utc_now

SCHEMA_VERSION = 3  # v3: shadow_log table (created by CREATE TABLE IF NOT EXISTS)

# Columns added after v1. init_schema() adds them to existing databases in place.
MIGRATIONS: list[tuple[int, str, str, str]] = [
    (2, "positions", "protection", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
    (2, "positions", "stop_client_order_id", "TEXT"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    evidence_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    args_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity TEXT,
    mode TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    creation_report_json TEXT,
    execution_report_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    method TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    role TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_qty TEXT NOT NULL,
    executed_qty TEXT NOT NULL,
    avg_price TEXT,
    exchange_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fee TEXT NOT NULL,
    fee_asset TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_balances (asset TEXT PRIMARY KEY, free TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS positions (
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    avg_entry TEXT NOT NULL,
    stop_price TEXT,
    take_profit TEXT,
    opened_at TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    last_checked_ms INTEGER NOT NULL,
    entry_fees TEXT NOT NULL DEFAULT '0',
    protection TEXT NOT NULL DEFAULT 'UNKNOWN',
    stop_client_order_id TEXT,
    PRIMARY KEY (mode, symbol)
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    quantity TEXT NOT NULL,
    avg_entry TEXT NOT NULL,
    avg_exit TEXT NOT NULL,
    fees TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    exit_reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_trade ON journal(trade_id);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    source_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    snippet TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Forward shadow log: one row per closed decision bar, written by `trader shadow-log`. Append-only.
-- A second insert for the same bar is silently ignored (also defeats INSERT OR REPLACE, which would
-- otherwise delete the old row without firing the delete trigger).
CREATE TABLE IF NOT EXISTS shadow_log (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER NOT NULL,
    bar_status TEXT NOT NULL CHECK (bar_status IN ('closed', 'missing')),
    logged_at TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
CREATE TRIGGER IF NOT EXISTS shadow_log_insert_once BEFORE INSERT ON shadow_log
WHEN EXISTS (
    SELECT 1 FROM shadow_log
    WHERE symbol = NEW.symbol AND interval = NEW.interval AND open_time_ms = NEW.open_time_ms
)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS shadow_log_no_update BEFORE UPDATE ON shadow_log
BEGIN SELECT RAISE(ABORT, 'shadow_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_log_no_delete BEFORE DELETE ON shadow_log
BEGIN SELECT RAISE(ABORT, 'shadow_log is append-only'); END;

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ connections

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. ``BEGIN IMMEDIATE`` takes the write lock up front."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            for _version, table, column, ddl in MIGRATIONS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    def schema_version(self) -> int:
        rows = self.query("SELECT value FROM meta WHERE key='schema_version'")
        return int(rows[0]["value"]) if rows else 0

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        with self.transaction() as conn:
            return conn.execute(sql, params).rowcount

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------ key/value

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value_json FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value_json"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, canonical_json(value), iso(utc_now())),
        )

    # ------------------------------------------------------------------ audit / evidence

    def record_evidence(
        self, evidence_id: str, tool: str, status: str, args: dict[str, Any], payload: Any, summary: str = ""
    ) -> None:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        self.execute(
            "INSERT INTO audit_events(evidence_id, ts, tool, status, args_json, payload_sha256, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, iso(utc_now()), tool, status, canonical_json(args), digest, summary[:500]),
        )

    def existing_evidence_ids(self, ids: set[str]) -> set[str]:
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.query(
            f"SELECT evidence_id FROM audit_events WHERE evidence_id IN ({placeholders})", tuple(ids)
        )
        return {row["evidence_id"] for row in rows}

    # ------------------------------------------------------------------ journal

    def journal_append(self, trade_id: str, event_type: str, payload: Any) -> dict[str, Any]:
        ts = iso(utc_now())
        payload_json = canonical_json(payload)
        with self.transaction() as conn:
            last = conn.execute("SELECT hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = last["hash"] if last else GENESIS_HASH
            digest = _journal_hash(prev_hash, ts, trade_id, event_type, payload_json)
            cursor = conn.execute(
                "INSERT INTO journal(ts, trade_id, event_type, payload_json, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (ts, trade_id, event_type, payload_json, prev_hash, digest),
            )
            return {"seq": cursor.lastrowid, "ts": ts, "hash": digest}

    def journal_events(self, trade_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if trade_id is None:
            rows = self.query("SELECT * FROM journal ORDER BY seq DESC LIMIT ?", (limit,))
            rows.reverse()
        else:
            rows = self.query(
                "SELECT * FROM journal WHERE trade_id=? ORDER BY seq LIMIT ?", (trade_id, limit)
            )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def journal_verify(self) -> tuple[bool, str]:
        """Recompute the hash chain. Returns (ok, message)."""
        prev = GENESIS_HASH
        count = 0
        for row in self.query("SELECT * FROM journal ORDER BY seq"):
            if row["prev_hash"] != prev:
                return False, f"chain broken at seq {row['seq']}: prev_hash mismatch"
            expected = _journal_hash(prev, row["ts"], row["trade_id"], row["event_type"], row["payload_json"])
            if expected != row["hash"]:
                return False, f"chain broken at seq {row['seq']}: content hash mismatch"
            prev = row["hash"]
            count += 1
        return True, f"{count} journal events verified"


def _journal_hash(prev_hash: str, ts: str | None, trade_id: str, event_type: str, payload_json: str) -> str:
    material = "\n".join([prev_hash, ts or "", trade_id, event_type, payload_json])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

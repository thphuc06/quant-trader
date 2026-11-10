"""Config, domain validation, time handling, storage and journal integrity."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from tests.conftest import REPO, synthetic_candles
from trader.config import RiskPolicy, Settings, TradingMode, load_app_config, load_risk_policy
from trader.domain import DecisionReport, TradeIntentRequest, jsonable
from trader.storage.candles import CandleStore, check_quality, drop_unclosed
from trader.storage.db import Database
from trader.timeutil import iso, parse_iso, to_ms, utc_now

# ----------------------------------------------------------------------------- config


def test_defaults_are_paper_and_live_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRADING_MODE", "LIVE_TRADING_ENABLED", "BINANCE_LIVE_API_KEY", "BINANCE_DEMO_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.trading_mode is TradingMode.PAPER
    assert settings.live_trading_enabled is False
    assert settings.credentials_for(TradingMode.LIVE) is None


def test_credentials_never_fall_back_between_environments() -> None:
    settings = Settings(
        binance_demo_api_key=SecretStr("demo-key"), binance_demo_api_secret=SecretStr("demo-secret")
    )
    assert settings.credentials_for(TradingMode.LIVE) is None
    assert settings.credentials_for(TradingMode.PAPER) is None
    assert settings.credentials_for(TradingMode.BINANCE_DEMO) is not None


def test_secrets_are_not_printed() -> None:
    settings = Settings(binance_live_api_secret=SecretStr("super-secret-value"))
    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.model_dump())


def test_shipped_configs_load_and_policy_is_provisional() -> None:
    app = load_app_config(REPO)
    loaded = load_risk_policy(REPO)
    assert app.symbols and app.decision_timeframe == "1h"
    assert loaded.policy.status == "provisional"
    assert loaded.policy.max_leverage == Decimal(1)
    assert set(loaded.policy.allowed_symbols) == set(app.symbols)
    assert len(loaded.sha256) == 64


def _policy_dict() -> dict:
    return yaml.safe_load((REPO / "configs" / "risk_policy.yaml").read_text(encoding="utf-8"))


def test_policy_rejects_unknown_keys_and_leverage_and_nonpositive_limits() -> None:
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate(
            {**_policy_dict(), "max_risk_per_trade_pcnt": "5"}
        )  # typo must not pass silently
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate({**_policy_dict(), "max_leverage": "3"})
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate({**_policy_dict(), "max_risk_per_trade_pct": "0"})


# ----------------------------------------------------------------------------- domain / time


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        to_ms(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        parse_iso("2026-01-01T00:00:00")
    assert iso(parse_iso("2026-01-01T07:00:00+07:00")) == "2026-01-01T00:00:00Z"


def _request(**overrides) -> dict:
    base = dict(
        decision_id="dec_1",
        symbol="btcusdt",
        side="BUY",
        stop_price="95",
        rationale="a sufficiently long rationale text",
        idempotency_key="key-12345",
    )
    return {**base, **overrides}


def test_intent_request_has_no_quantity_and_only_scales_risk_down() -> None:
    assert TradeIntentRequest(**_request()).symbol == "BTCUSDT"
    with pytest.raises(ValidationError):
        TradeIntentRequest(**_request(quantity="5"))
    with pytest.raises(ValidationError):
        TradeIntentRequest(**_request(size_scale="1.5"))
    with pytest.raises(ValidationError):
        TradeIntentRequest(**_request(order_type="LIMIT"))  # limit without price


def _report(**overrides) -> dict:
    base = dict(
        as_of=utc_now().isoformat(),
        symbol="BTCUSDT",
        horizon="24h",
        decision="NO_TRADE",
        reason_for_no_trade="no validated signal",
    )
    return {**base, **overrides}


def test_decision_report_rules() -> None:
    DecisionReport.model_validate(_report())
    with pytest.raises(ValidationError):
        DecisionReport.model_validate(_report(reason_for_no_trade=None))
    with pytest.raises(ValidationError):  # candidate without counter-evidence / stop / invalidation
        DecisionReport.model_validate(_report(decision="LONG_CANDIDATE"))
    with pytest.raises(ValidationError):  # shorts cannot request execution on spot
        DecisionReport.model_validate(
            _report(decision="SHORT_CANDIDATE", action_requested="REQUEST_RISK_VALIDATION")
        )
    with pytest.raises(ValidationError):
        DecisionReport.model_validate(_report(human_approval_required=False))
    with pytest.raises(ValidationError):
        DecisionReport.model_validate(_report(as_of="2026-01-01T00:00:00"))


def test_jsonable_keeps_decimal_exact() -> None:
    assert jsonable({"a": Decimal("0.10"), "b": Decimal("1E+2"), "c": [Decimal("5")]}) == {
        "a": "0.1",
        "b": "100",
        "c": ["5"],
    }


# ----------------------------------------------------------------------------- storage


def test_schema_initialises_and_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    db.init_schema()
    db.init_schema()
    assert db.schema_version() == 2


def test_journal_is_append_only_and_tamper_evident(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    db.init_schema()
    for i in range(3):
        db.journal_append("trade_1", "event", {"i": i, "price": Decimal("1.5")})
    assert db.journal_verify()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE journal SET payload_json='{}' WHERE seq=2")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM journal WHERE seq=1")
    # someone with file access who removes the trigger is still caught by the hash chain
    conn = sqlite3.connect(db.path)
    conn.execute("DROP TRIGGER journal_no_update")
    conn.execute("UPDATE journal SET payload_json='{\"i\":99}' WHERE seq=2")
    conn.commit()
    conn.close()
    ok, message = db.journal_verify()
    assert not ok and "seq 2" in message


def test_evidence_ids_are_tracked(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    db.init_schema()
    db.record_evidence("ev_1", "tool", "ok", {"a": 1}, {"x": 1})
    assert db.existing_evidence_ids({"ev_1", "ev_missing"}) == {"ev_1"}


def test_candle_upsert_deduplicates_and_sorts(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    frame = synthetic_candles(50)
    store.upsert("BTCUSDT", "1h", frame.iloc[10:30])
    merged = store.upsert("BTCUSDT", "1h", frame.sample(frac=1.0, random_state=1))
    assert len(merged) == 50 and merged["open_time"].is_monotonic_increasing
    with pytest.raises(ValueError):
        store.load("../etc", "1h")


def test_quality_flags_gaps_duplicates_stale_and_invalid() -> None:
    frame = synthetic_candles(100)
    now = int(frame["close_time"].iloc[-1]) + 1
    assert check_quality(frame, "X", "1h", now).ok
    gappy = frame.drop(index=[40, 41, 42]).reset_index(drop=True)
    report = check_quality(gappy, "X", "1h", now)
    assert report.gaps == 1 and report.missing_bars == 3 and not report.ok
    import pandas as pd

    assert check_quality(pd.concat([frame, frame.iloc[[5]]]), "X", "1h", now).duplicates == 1
    assert "stale" in check_quality(frame, "X", "1h", now + 5 * 3_600_000).issues[0]
    broken = frame.copy()
    broken.loc[3, "high"] = broken.loc[3, "low"] / 2
    assert check_quality(broken, "X", "1h", now).invalid_rows == 1
    assert not check_quality(frame.iloc[0:0], "X", "1h", now).ok


def test_unclosed_candle_is_dropped() -> None:
    frame = synthetic_candles(10)
    now = int(frame["close_time"].iloc[-1]) - 10  # last bar still open
    assert len(drop_unclosed(frame, now)) == 9


def test_schema_v1_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An operator's existing database must keep working after the protection columns were added."""
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE positions (mode TEXT NOT NULL, symbol TEXT NOT NULL, quantity TEXT NOT NULL, avg_entry TEXT NOT NULL,
            stop_price TEXT, take_profit TEXT, opened_at TEXT NOT NULL, intent_id TEXT NOT NULL, last_checked_ms INTEGER NOT NULL,
            entry_fees TEXT NOT NULL DEFAULT '0', PRIMARY KEY (mode, symbol));
        INSERT INTO positions VALUES ('BINANCE_DEMO', 'BTCUSDT', '0.5', '100', '98', NULL, '2026-11-10T00:00:00Z', 'int_1', 0, '0');
        """
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.init_schema()
    db.init_schema()  # idempotent
    assert db.schema_version() == 2
    row = db.query("SELECT * FROM positions")[0]
    assert row["quantity"] == "0.5" and row["protection"] == "UNKNOWN" and row["stop_client_order_id"] is None

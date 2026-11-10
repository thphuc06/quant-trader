#!/usr/bin/env python3
"""SessionStart hook (stdlib only): inject a factual status banner. Never prints secrets.

Runs on startup, resume, clear and after compaction, so the operating state survives context loss.
Any failure degrades to a short banner; it must never block the session.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def read_key(path: Path, key: str) -> str | None:
    """Read one non-secret KEY=value line. Only called for TRADING_MODE and LIVE_TRADING_ENABLED."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(rf"\s*{re.escape(key)}\s*=\s*['\"]?([A-Za-z_]+)", line)
            if match:
                return match.group(1)
    except OSError:
        pass
    return None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())
    role = os.environ.get("TRADER_SESSION_ROLE", "trader").strip().lower()
    mode = os.environ.get("TRADING_MODE") or read_key(project / ".env", "TRADING_MODE") or "PAPER"
    live_flag = (
        os.environ.get("LIVE_TRADING_ENABLED")
        or read_key(project / ".env", "LIVE_TRADING_ENABLED")
        or "false"
    )

    lines = [f"Session role: {role.upper()}. Trading mode: {mode}. LIVE_TRADING_ENABLED={live_flag}."]
    try:
        text = (project / "configs" / "risk_policy.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version:\s*"?([^"\n]+)', text, re.M)
        status = re.search(r"^status:\s*(\w+)", text, re.M)
        lines.append(
            f"Risk policy v{version.group(1) if version else '?'} ({status.group(1) if status else '?'})."
        )
    except OSError:
        lines.append("Risk policy file is missing: no new positions can be opened.")

    data_dir = Path(os.environ.get("TRADER_DATA_DIR") or project / "data")
    db_path = data_dir / "trader.sqlite"
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
            row = conn.execute("SELECT value_json FROM kv WHERE key='kill_switch'").fetchone()
            if row and json.loads(row[0]).get("engaged"):
                lines.append("KILL SWITCH IS ENGAGED: new positions are blocked.")
            open_intents = conn.execute(
                "SELECT COUNT(*), SUM(status='UNKNOWN') FROM intents WHERE status IN ('PENDING_APPROVAL','EXECUTING','UNKNOWN')"
            ).fetchone()
            positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            conn.close()
            lines.append(
                f"Open intents: {open_intents[0] or 0} (unknown order state: {open_intents[1] or 0}). Open positions: {positions}."
            )
        except (sqlite3.Error, ValueError):
            lines.append("Operational database could not be read; rely on get_system_status.")
    else:
        lines.append("No operational database yet (first run).")

    if role == "builder":
        lines.append(
            "Builder session: you may change code and docs. Human-only commands (approve, live-unlock, kill-switch release, lesson promote) stay blocked."
        )
    else:
        lines.append(
            "Trader session: run /trading-desk before anything else. Do not modify code, configs, risk policy or skills. "
            "Never open a trade unless the user explicitly asks; analysis requests end with a recorded decision report."
        )
    if event.get("source") in {"compact", "resume"}:
        lines.append(
            "Context was compacted or resumed: re-read state/session-summary.md and call get_system_status and sync_positions before continuing."
        )
    summary = project / "state" / "session-summary.md"
    try:
        has_notes = summary.exists() and "none yet" not in summary.read_text(encoding="utf-8")
    except OSError:
        has_notes = False
    if has_notes:
        lines.append("A previous session left notes in state/session-summary.md; read them first.")

    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": " ".join(lines)}}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # never block a session because the banner failed
        sys.exit(0)

#!/usr/bin/env python3
"""PostToolUse hook for mcp__trading__* (stdlib only): append a redacted line to data/audit/claude_tool_calls.jsonl.

This is the Claude-side record of what was asked. The authoritative audit (with evidence ids and payload
hashes) is written by the backend. Failures here never block anything.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SENSITIVE_KEY = re.compile(r"(secret|token|password|api[_-]?key|signature|authorization)", re.IGNORECASE)
SENSITIVE_VALUE = re.compile(r"(tvly-[A-Za-z0-9_-]{8,}|[A-Za-z0-9]{48,})")


def redact(value, depth: int = 0):
    if depth > 6:
        return "<truncated>"
    if isinstance(value, dict):
        return {k: ("<redacted>" if SENSITIVE_KEY.search(str(k)) else redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return SENSITIVE_VALUE.sub("<redacted>", value)[:500]
    return value


def main() -> None:
    event = json.load(sys.stdin)
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())
    response = event.get("tool_response")
    status = evidence_id = None
    text = json.dumps(response, default=str) if response is not None else ""
    match = re.search(r'\\?"status\\?":\s*\\?"(\w+)', text)
    if match:
        status = match.group(1)
    match = re.search(r"(ev_[0-9a-f]{20})", text)
    if match:
        evidence_id = match.group(1)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": event.get("session_id"),
        "tool": event.get("tool_name"),
        "input": redact(event.get("tool_input") or {}),
        "status": status,
        "evidence_id": evidence_id,
    }
    directory = Path(os.environ.get("TRADER_DATA_DIR") or project / "data") / "audit"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "claude_tool_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

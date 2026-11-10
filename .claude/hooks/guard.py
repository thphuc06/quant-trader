#!/usr/bin/env python3
"""PreToolUse guard (stdlib only).

Role comes from the environment variable TRADER_SESSION_ROLE:
  trader  (default) - analyst/operator session: no edits to code, config, policy, skills or data.
  builder           - development session: edits allowed, human-only commands still blocked.

This hook is a convenience layer. Every rule that protects money is enforced again in the Python backend.
Exit code 2 blocks the tool call and shows stderr to Claude.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

TRADER_WRITABLE = ("state/", "knowledge/lessons_pending_review/", "knowledge/trade_casebook/", "reports/")
ALWAYS_PROTECTED_FILES = (".env", "data/live_unlock.json", "data/trader.sqlite")

# Commands only the human operator may run, in their own terminal.
HUMAN_ONLY = [
    (r"\btrader(\.cli)?\s+approve\b", "approving a trade intent"),
    (r"\blive-unlock\b", "unlocking live trading"),
    (r"\bkill-switch\s+release\b", "releasing the kill switch"),
    (r"\blesson\s+promote\b", "promoting a lesson"),
    (r"--unlock-holdout\b", "unlocking the research holdout"),
    (r"record_cli_approval|release_kill_switch|_insert_approval", "calling approval internals directly"),
    (r"live_unlock\.json", "touching the live unlock file"),
    (r"LIVE_TRADING_ENABLED|TRADING_MODE\s*=\s*['\"]?LIVE", "changing the trading mode to live"),
]
# Things no session should do from a shell.
FORBIDDEN = [
    (r"(?<![\w.])\.env(?!\.example)(?![\w])", "reading or writing .env (secrets)"),
    (r"/api/v3/order|/sapi/|/fapi/v1/order|/api/v3/account", "calling Binance account/order endpoints outside the backend"),
    (r"trader\.sqlite", "accessing the operational database directly"),
    (r"X-MBX-APIKEY|API_SECRET|api_secret", "handling exchange credentials"),
]
WRITE_VERBS = r"(>|\bSet-Content\b|\bOut-File\b|\bAdd-Content\b|\bsed\s+-i\b|\btee\b|\brm\b|\bRemove-Item\b|\bmv\b|\bMove-Item\b|\bcp\b|\bCopy-Item\b|\bNew-Item\b|\bgit\s+(checkout|restore|reset|apply|stash)\b|\bwrite_text\b|\bopen\()"
TRADER_PROTECTED_PATHS = r"(configs[/\\]|src[/\\]trader|\.claude[/\\]|\.mcp\.json|CLAUDE\.md|risk_policy\.yaml|knowledge[/\\]approved_lessons|research[/\\]experiments)"


def deny(reason: str) -> None:
    print(f"BLOCKED by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def relative_path(raw: str, project: Path) -> str | None:
    """Project-relative POSIX path, or None when the path is outside the project."""
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = project / path
        return PurePosixPath(os.path.normpath(path).replace("\\", "/")).relative_to(
            PurePosixPath(os.path.normpath(project).replace("\\", "/"))
        ).as_posix()
    except (ValueError, OSError):
        return None


def check_edit(tool_input: dict, role: str, project: Path) -> None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    rel = relative_path(str(raw), project)
    if rel is None:
        if role == "trader":
            deny("trader sessions may not write files outside the project")
        return
    lowered = rel.lower()
    name = lowered.rsplit("/", 1)[-1]
    if lowered in ALWAYS_PROTECTED_FILES or (name.startswith(".env") and name != ".env.example") or lowered.startswith("data/"):
        deny(f"{rel} holds secrets or runtime state and is never edited by Claude")
    if role != "trader":
        return
    if not lowered.startswith(TRADER_WRITABLE):
        deny(
            f"trader sessions do not modify {rel}. Writable: {', '.join(TRADER_WRITABLE)}. "
            "Code, strategy, risk policy and skills change only in a builder session "
            "(start Claude Code with TRADER_SESSION_ROLE=builder) and through a reviewed git commit."
        )


def check_shell(command: str, role: str) -> None:
    for pattern, what in HUMAN_ONLY:
        if re.search(pattern, command, re.IGNORECASE):
            deny(f"{what} is a human-only action. Ask the operator to run it in their own terminal.")
    for pattern, what in FORBIDDEN:
        if re.search(pattern, command):
            deny(f"{what} is not allowed from a shell. Use the trading MCP tools.")
    if role == "trader" and re.search(TRADER_PROTECTED_PATHS, command, re.IGNORECASE) and re.search(WRITE_VERBS, command):
        deny("trader sessions do not modify code, configuration, policy, skills or the experiment registry from a shell.")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        deny("could not parse hook input (failing closed)")
        return
    role = os.environ.get("TRADER_SESSION_ROLE", "trader").strip().lower()
    if role not in {"trader", "builder"}:
        role = "trader"
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if tool in {"Edit", "Write", "NotebookEdit", "MultiEdit"}:
        check_edit(tool_input, role, project)
    elif tool in {"Bash", "PowerShell"}:
        check_shell(str(tool_input.get("command", "")), role)
    sys.exit(0)


if __name__ == "__main__":
    main()

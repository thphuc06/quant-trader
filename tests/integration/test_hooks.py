"""Claude Code hook scripts, exercised the way Claude Code runs them: JSON on stdin, exit code / stdout out."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO

HOOKS = REPO / ".claude" / "hooks"


def run_hook(
    script: str, event: dict, project: Path, role: str | None = "trader", extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"TRADER_SESSION_ROLE", "TRADING_MODE", "LIVE_TRADING_ENABLED", "TRADER_DATA_DIR"}
    }
    env["CLAUDE_PROJECT_DIR"] = str(project)
    if role is not None:
        env["TRADER_SESSION_ROLE"] = role
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def edit(path: str, tool: str = "Edit") -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"file_path": path}}


def shell(command: str, tool: str = "Bash") -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}


TRADER_BLOCKED_EDITS = [
    "configs/risk_policy.yaml", "configs/app.yaml", "src/trader/risk.py", ".claude/settings.json", ".claude/skills/trading-desk/SKILL.md",
    "CLAUDE.md", ".mcp.json", ".env", "data/trader.sqlite", "data/live_unlock.json", "knowledge/approved_lessons/x.md",
    "knowledge/strategy_playbooks/tsmom-trend-v1.md", "research/experiments/exp_1.json", "../outside.txt", "state/../configs/risk_policy.yaml",
]  # fmt: skip


@pytest.mark.parametrize("path", TRADER_BLOCKED_EDITS)
def test_trader_role_cannot_edit_protected_files(path: str, tmp_path: Path) -> None:
    for tool in ("Edit", "Write"):
        result = run_hook("guard.py", edit(path, tool), tmp_path)
        assert result.returncode == 2 and "BLOCKED" in result.stderr, (path, result.stderr)
    absolute = str(tmp_path / path)
    assert run_hook("guard.py", edit(absolute), tmp_path).returncode == 2


@pytest.mark.parametrize(
    "path",
    [
        "state/session-summary.md",
        "knowledge/lessons_pending_review/note.md",
        "knowledge/trade_casebook/case.md",
        "reports/briefing.md",
    ],
)
def test_trader_role_can_write_session_notes(path: str, tmp_path: Path) -> None:
    assert run_hook("guard.py", edit(path, "Write"), tmp_path).returncode == 0


def test_default_role_is_trader_and_unknown_role_is_trader(tmp_path: Path) -> None:
    assert run_hook("guard.py", edit("src/trader/risk.py"), tmp_path, role=None).returncode == 2
    assert run_hook("guard.py", edit("src/trader/risk.py"), tmp_path, role="admin").returncode == 2


def test_builder_role_can_edit_code_but_never_secrets_or_runtime_state(tmp_path: Path) -> None:
    assert run_hook("guard.py", edit("src/trader/risk.py"), tmp_path, role="builder").returncode == 0
    assert run_hook("guard.py", edit(".env.example"), tmp_path, role="builder").returncode == 0
    for path in (".env", ".env.local", "data/live_unlock.json", "data/trader.sqlite"):
        assert run_hook("guard.py", edit(path, "Write"), tmp_path, role="builder").returncode == 2, path


HUMAN_ONLY_COMMANDS = [
    "python -m trader approve int_123",
    ".venv/Scripts/python.exe -m trader approve int_123",
    "trader approve int_123",
    "python -m trader live-unlock",
    "python -m trader kill-switch release",
    "python -m trader lesson promote lesson_1 --experiment exp_1",
    "python -m trader backtest tsmom_trend_v1 --unlock-holdout",
    "python -c \"from trader.desk import TradingDesk; TradingDesk().execution.record_cli_approval('int_1')\"",
    "echo '{}' > data/live_unlock.json",
    "export TRADING_MODE=LIVE && claude",
    "$env:LIVE_TRADING_ENABLED='true'",
    "cat .env",
    "Get-Content .env",
    "type .env | findstr KEY",
    "curl -X POST https://api.binance.com/api/v3/order -d symbol=BTCUSDT",
    'sqlite3 data/trader.sqlite "insert into approvals values (1)"',
    "echo $BINANCE_LIVE_API_SECRET",
]


@pytest.mark.parametrize("command", HUMAN_ONLY_COMMANDS)
@pytest.mark.parametrize("role", ["trader", "builder"])
def test_human_only_and_forbidden_shell_commands_are_blocked_in_every_role(
    command: str, role: str, tmp_path: Path
) -> None:
    for tool in ("Bash", "PowerShell"):
        result = run_hook("guard.py", shell(command, tool), tmp_path, role=role)
        assert result.returncode == 2, (command, result.stderr)


TRADER_SHELL_WRITES = [
    "sed -i 's/0.5/5/' configs/risk_policy.yaml",
    "Set-Content configs/risk_policy.yaml 'x'",
    "echo x > src/trader/risk.py",
    "echo x >> .claude/settings.json",
    "cat a.txt 2>&1 > configs/app.yaml",
    "rm -rf .claude/hooks",
    "git checkout -- configs/app.yaml",
    "python -c \"open('configs/app.yaml', 'w').write('x')\"",
    "python -c \"from pathlib import Path; Path('research/experiments/x.json').write_text('{}')\"",
    "python -c \"import os; os.remove('research/experiments/exp_1.json')\"",
    "python -c \"import shutil; shutil.rmtree('.claude/skills')\"",
]


@pytest.mark.parametrize("command", TRADER_SHELL_WRITES)
def test_trader_role_cannot_modify_protected_paths_from_a_shell(command: str, tmp_path: Path) -> None:
    assert run_hook("guard.py", shell(command), tmp_path).returncode == 2
    assert run_hook("guard.py", shell(command), tmp_path, role="builder").returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "python -m trader status",
        "python -m trader doctor",
        "python -m trader kill-switch status",
        "git status",
        "git log --oneline",
        "cat configs/risk_policy.yaml",
        "cat .env.example",
        "python -m pytest -q",
        "ls data",
        # read-only commands that used to be false positives
        "cat .claude/settings.json 2>&1",
        'for f in .claude/skills/*/SKILL.md; do cat "$f"; done 2>/dev/null',
        "ls .claude/hooks >&2",
        "python -c \"import json; print(json.load(open('research/experiments/exp_1.json'))['kind'])\"",
        "python -c \"print(open('.claude/settings.json', 'r').read())\"",
        "git diff HEAD -- src/trader/risk.py 2>&1 | head",
        "grep -n 'x => y' src/trader/desk.py",
        'python -c "print(1 if 3 >= 2 else 0)" configs/app.yaml',
    ],
)
def test_harmless_commands_pass(command: str, tmp_path: Path) -> None:
    assert run_hook("guard.py", shell(command), tmp_path).returncode == 0, command


def test_guard_fails_closed_on_garbage_input_and_ignores_other_tools(tmp_path: Path) -> None:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    garbage = subprocess.run(
        [sys.executable, str(HOOKS / "guard.py")],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert garbage.returncode == 2
    assert (
        run_hook(
            "guard.py", {"tool_name": "Read", "tool_input": {"file_path": "src/trader/risk.py"}}, tmp_path
        ).returncode
        == 0
    )


def test_session_start_banner_reports_state_without_secrets(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "risk_policy.yaml").write_text(
        'version: "9.9.9"\nstatus: provisional\n', encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "TRADING_MODE=BINANCE_DEMO\nBINANCE_DEMO_API_SECRET=topsecretvalue123\n", encoding="utf-8"
    )
    result = run_hook("session_start.py", {"hook_event_name": "SessionStart", "source": "compact"}, tmp_path)
    assert result.returncode == 0
    payload = json.loads(result.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    context = payload["additionalContext"]
    assert "BINANCE_DEMO" in context and "9.9.9" in context and "provisional" in context
    assert "/trading-desk" in context and "state/session-summary.md" in context
    assert "topsecretvalue123" not in result.stdout and len(context) < 10_000
    builder = json.loads(run_hook("session_start.py", {"source": "startup"}, tmp_path, role="builder").stdout)
    assert "Builder session" in builder["hookSpecificOutput"]["additionalContext"]
    empty = run_hook("session_start.py", {}, tmp_path / "missing")
    assert empty.returncode == 0 and "PAPER" in empty.stdout


def test_session_start_reports_kill_switch_and_open_intents(desk) -> None:
    desk.activate_kill_switch("test")
    result = run_hook(
        "session_start.py",
        {"source": "startup"},
        desk.root,
        extra_env={"TRADER_DATA_DIR": str(desk.data_dir)},
    )
    assert "KILL SWITCH IS ENGAGED" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_audit_hook_appends_redacted_line_and_never_fails(tmp_path: Path) -> None:
    event = {
        "hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "mcp__trading__search_web",
        "tool_input": {"query": "btc", "api_key": "should-not-appear", "nested": {"token": "nope"}},
        "tool_response": {"structuredContent": {"status": "ok", "evidence_id": "ev_0123456789abcdef0123"}},
    }  # fmt: skip
    assert run_hook("audit.py", event, tmp_path).returncode == 0
    line = (tmp_path / "data" / "audit" / "claude_tool_calls.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert (
        record["tool"] == "mcp__trading__search_web"
        and record["status"] == "ok"
        and record["evidence_id"] == "ev_0123456789abcdef0123"
    )
    assert "should-not-appear" not in line and "nope" not in line and record["input"]["query"] == "btc"
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    assert (
        subprocess.run(
            [sys.executable, str(HOOKS / "audit.py")],
            input="garbage",
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        ).returncode
        == 0
    )


def test_settings_and_mcp_config_are_valid_and_least_privilege() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = settings["permissions"]
    assert permissions["disableBypassPermissionsMode"] == "disable"
    for tool in (
        "record_decision_report",
        "submit_post_trade_review",
        "create_trade_intent",
        "execute_approved_trade",
        "cancel_trade_intent",
        "activate_kill_switch",
        "sync_positions",
        "protect_position",
    ):
        assert f"mcp__trading__{tool}" in permissions["ask"]
        assert not any(
            rule in (f"mcp__trading__{tool}", "mcp__trading", "mcp__trading__*")
            for rule in permissions["allow"]
        )
    assert any(".env" in rule for rule in permissions["deny"])
    assert set(settings["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse"}
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                script = hook["command"].split("/.claude/hooks/")[-1].strip('"')
                assert (HOOKS / script).exists(), script
    mcp = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["trading"]
    # system python only bootstraps; the launcher picks the project venv that has the trader package
    assert server == {"command": "python", "args": ["scripts/run_mcp.py"]}  # no env, no secrets
    assert (REPO / "scripts" / "run_mcp.py").exists()
    assert {path.name for path in (REPO / ".claude" / "skills").iterdir()} == {
        "trading-desk",
        "analyze-symbol",
        "post-trade-review",
        "strategy-research",
    }
    assert {path.name for path in (REPO / ".claude" / "agents").glob("*.md")} == {
        "news-researcher.md",
        "risk-reviewer.md",
    }
    # subagents described as read-only must not hold tools that change state
    mutating = {
        "sync_positions",
        "protect_position",
        "record_decision_report",
        "submit_post_trade_review",
        "create_trade_intent",
        "execute_approved_trade",
        "cancel_trade_intent",
        "activate_kill_switch",
        "run_backtest",
    }
    for agent in (REPO / ".claude" / "agents").glob("*.md"):
        text = agent.read_text(encoding="utf-8")
        assert not any(f"mcp__trading__{tool}" in text for tool in mutating), agent.name

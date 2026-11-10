"""The MCP launcher used by .mcp.json must start a working server over real stdio, from any python."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from tests.conftest import REPO

LAUNCHER = REPO / "scripts" / "run_mcp.py"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_launcher_serves_mcp_over_stdio(tmp_path: Path) -> None:
    env = {**os.environ, "TRADER_DATA_DIR": str(tmp_path / "data"), "TRADING_MODE": "PAPER"}
    params = StdioServerParameters(command=sys.executable, args=[str(LAUNCHER)], env=env, cwd=str(tmp_path))
    async with Client(params, read_timeout_seconds=60) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"get_system_status", "get_open_positions", "sync_positions", "create_trade_intent"} <= names
        status = (await client.call_tool("get_system_status", {})).structured_content
        assert status["data"]["trading_mode"] == "PAPER" and status["data"]["live"]["unlocked"] is False
    assert (tmp_path / "data" / "trader.sqlite").exists()  # the real repo data dir was not used


def test_launcher_without_any_environment_explains_setup(tmp_path: Path) -> None:
    """Simulate a machine with no venv and no installed package: a clear stderr message, nothing on stdout."""
    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / "scripts" / "run_mcp.py").write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in {"TRADER_PYTHON", "PYTHONPATH"}}
    # -I isolates from user site-packages and the CWD so an editable install of trader is not found
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(fake_repo / "scripts" / "run_mcp.py")],
        capture_output=True, text=True, env=env, timeout=60, cwd=tmp_path,
    )  # fmt: skip
    assert result.returncode == 1
    assert result.stdout == ""  # stdout is reserved for the MCP protocol
    assert "requirements.lock" in result.stderr


def test_launcher_rejects_missing_override(tmp_path: Path) -> None:
    env = {**os.environ, "TRADER_PYTHON": str(tmp_path / "nope" / "python.exe")}
    result = subprocess.run(
        [sys.executable, str(LAUNCHER)], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 1 and result.stdout == ""

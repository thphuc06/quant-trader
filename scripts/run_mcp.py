#!/usr/bin/env python3
"""Start the ``trading`` MCP server with the project's virtualenv (stdlib only).

Claude Code launches this with whatever ``python`` is on PATH. That interpreter usually does not have the
``trader`` package, so this script finds the project interpreter and runs the server with it:

1. ``$TRADER_PYTHON`` if set,
2. ``.venv/Scripts/python.exe`` (Windows) or ``.venv/bin/python`` (POSIX),
3. the current interpreter, if it can import ``trader``.

stdout belongs to the MCP protocol: this script never prints to it. Errors go to stderr.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_CANDIDATES = (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python")

SETUP_HELP = (
    "trading MCP server: no Python environment with the 'trader' package was found.\n"
    "Set it up from the repository root:\n"
    "  python -m venv .venv\n"
    "  .venv/Scripts/python -m pip install -r requirements.lock   (POSIX: .venv/bin/python)\n"
    "  .venv/Scripts/python -m pip install -e . --no-deps\n"
    "or point TRADER_PYTHON at an interpreter that has it installed."
)


def find_python() -> str | None:
    override = os.environ.get("TRADER_PYTHON", "").strip()
    if override:
        return override if Path(override).exists() else None
    for candidate in VENV_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    if importlib.util.find_spec("trader") is not None:
        return sys.executable
    return None


def main() -> int:
    python = find_python()
    if python is None:
        print(SETUP_HELP, file=sys.stderr)
        return 1
    env = dict(os.environ)
    env.setdefault("TRADER_HOME", str(ROOT))
    command = [python, "-m", "trader.mcp_server"]
    if os.name == "posix":
        # Replace this process so signals from Claude Code reach the server directly.
        os.chdir(ROOT)
        os.execve(python, command, env)
    # On Windows exec does not replace the process; run as a child with inherited stdio instead.
    # Claude Code terminates the whole process tree when the session ends.
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

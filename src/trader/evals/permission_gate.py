"""Permission gate for headless decision-eval sessions (``--permission-prompt-tool mcp__eval_gate__decide``).

In ``claude -p`` mode a tool whose project rule is ``ask`` would be denied, and an ``ask`` rule outranks
``--allowedTools``. The decision-eval sessions must be able to record their decision report (in PAPER mode, in a
separate data directory), and nothing else that needs permission. This server answers every permission prompt of
those sessions: it allows exactly ``record_decision_report`` and denies everything else, including every order
tool. Read-only tools never reach it (they are allowed without a prompt).

stdout belongs to the MCP protocol: never print.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

ALLOWED = frozenset({"mcp__trading__record_decision_report"})

gate = MCPServer(
    "eval_gate", instructions="Permission gate for decision-eval sessions. Not for interactive use."
)


def decision_for(tool_name: str, tool_input: dict[str, Any] | None) -> dict[str, Any]:
    if tool_name in ALLOWED:
        return {"behavior": "allow", "updatedInput": tool_input or {}}
    return {
        "behavior": "deny",
        "message": f"{tool_name} is not permitted in a decision-eval session; only the decision report may be recorded.",
    }


@gate.tool(structured_output=False)  # Claude Code expects exactly one text block, no structuredContent wrapper
def decide(tool_name: str, input: dict[str, Any] | None = None, tool_use_id: str | None = None) -> str:
    """Answer one permission prompt. Returns the JSON text Claude Code expects ({behavior, updatedInput|message})."""
    return json.dumps(decision_for(tool_name, input))


def main() -> None:
    gate.run(transport="stdio")


if __name__ == "__main__":
    main()

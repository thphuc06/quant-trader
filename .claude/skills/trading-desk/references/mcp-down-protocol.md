# When the `trading` MCP server is unavailable

Symptoms: no `mcp__trading__*` tools, a connection error, or every call failing with a transport error.

Do:
1. Stop the market task. Say plainly that the trading backend is not connected, so no market data, risk check or
   journal action is possible.
2. Tell the operator how to check: run `/mcp` in Claude Code (server `trading` should be `connected`); from a
   terminal, `python -m trader doctor`. Setup steps are in `docs/operating-the-trading-desk.md`.
3. Offer non-market help only (explaining the system, reviewing documents).

Do not:
- quote prices, regimes or signals from memory or from a web search as if they were current desk data;
- run `python -m trader ...`, import `trader` in a script, or call Binance URLs from a shell or WebFetch to work
  around the outage (the backend's risk checks and audit trail would be bypassed);
- create files that pretend to be decision reports.

If tools fail with a permission message ("haven't granted it yet") rather than a connection error, the server is
up: the operator has not approved the tool or has not trusted this folder yet. Say which tool was denied.

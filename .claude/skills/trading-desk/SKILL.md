---
name: trading-desk
description: Start a supervised trading-desk session - check system, risk, positions and protection state, then ask what to work on. Run this first in every session.
---

# Trading desk start-up

Goal: an accurate picture of the desk before any analysis. No trading in this skill.

## 0. Is the backend there?

The `trading` MCP tools (`mcp__trading__*`) must be available. If they are missing or a call fails with a
connection error, follow [references/mcp-down-protocol.md](references/mcp-down-protocol.md) and stop.

## 1. Read state (in this order)

1. `get_system_status` — mode, live lock, risk policy version/status, kill switch, journal integrity, open intents.
2. `get_risk_state` — limits and current usage.
3. `sync_positions` — settles stops/targets that triggered while no session was open and reconciles exchange
   stops. It changes records, so the operator is asked to approve it; if they decline, use `get_open_positions`
   (read-only) and say that the view may be stale.
4. If `state/session-summary.md` exists, read it.

## 2. Report (short, factual)

- Mode (expect PAPER unless the operator said otherwise; never change it) and whether execution is permitted.
- Risk policy status. `provisional` means the limits are unreviewed guesses.
- Kill switch: engaged or not, and why.
- Open positions with `protection`. **UNPROTECTED or UNKNOWN comes first in the report**: say that new entries are
  blocked and offer `protect_position` (it asks the operator).
- Open intents, especially any in `UNKNOWN` order state (call `get_trade_intent` for each to reconcile).
- Data or journal warnings exactly as returned.
- Validation state: no strategy has passed the research gates unless `get_system_status` says otherwise.

## 3. Ask

Ask what the operator wants: a symbol analysis (`/analyze-symbol`), a market overview (`get_market_overview`),
research (`/strategy-research`) or a trade review (`/post-trade-review`). Do not create intents here.

## References

- [references/tool-usage-map.md](references/tool-usage-map.md) — which tool answers which question; research budget.
- [references/evidence-standards.md](references/evidence-standards.md) — how to label and cite evidence.
- [references/mcp-down-protocol.md](references/mcp-down-protocol.md) — what to do when the backend is unreachable.

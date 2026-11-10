# ADR 0005 — Intents, server-side sizing, human-only approval, locked live mode

Status: accepted · 2026-11-10

## Decision
1. **No order tool.** Claude can only create a *trade intent*: symbol, side, order type, optional limit price, stop,
   optional target, a risk scale ≤ 1, rationale, decision ID and an idempotency key.
2. **The backend computes quantity** from equity, stop distance, costs, policy and exchange filters.
3. **Validation twice**: at intent creation (stored as an immutable report) and again at execution with fresh data.
4. **Approval**
   - PAPER: the Claude Code permission prompt on `execute_approved_trade` (rule `ask`) is the human approval.
   - BINANCE_DEMO / LIVE: `python -m trader approve <intent_id>` — refuses without an interactive terminal, shows the
     exact parameters, requires typing the symbol. The approval is single-use, expires, and is bound to a hash of the
     order parameters. No token passes through the model.
5. **Idempotency**: `clientOrderId` is derived from the intent ID. Unknown outcome → query by that ID; never resend blindly.
6. **Modes**: `PAPER` (default) / `BINANCE_DEMO` / `LIVE`, each with its own base URL and credential variables, no fallback.
7. **Live lock**: all of `TRADING_MODE=LIVE`, `LIVE_TRADING_ENABLED=true`, live credentials, risk policy
   `status: approved`, and an unexpired unlock file written by `python -m trader live-unlock` (TTY only).
8. **Kill switch**: engage via MCP or CLI; release via CLI only; never auto-closes positions.
9. No withdraw/transfer/margin/leverage code paths exist.

## Why not the prompt's "one-time approval token"
A token handed to the LLM to pass back is a secret inside the context window: it can be replayed, logged, or leaked
by injected content. Recording the approval out-of-band and letting the server consume it removes that path.

## Known limits
- Same-OS-user boundary (threat model T3): the TTY requirement stops Claude's tools and scripts, not a process that
  writes to the database directly. Before real funds, move approval and credentials to a separate account/process.
- The Demo/Live adapter is verified against mocked HTTP only. The first Demo session must be supervised step by step.
- Exchange-side protection is a single `STOP_LOSS` sell (a market order once triggered); targets are not placed on the
  exchange. See the 2026-11-10 amendment below.

## Amendment 2026-11-10 — protective stops

Audit finding: the original `STOP_LOSS_LIMIT` (limit 0.5% under the trigger) could be skipped by a gap and leave the
position unprotected, while sizing assumes the loss is capped near the stop; a failed stop only produced a warning;
an exit cancelled the stop before selling, so a failed sell left a naked position.

Decision:
1. Stops are `STOP_LOSS` (stop-market). Accepted cost: slippage beyond the trigger in fast markets. Symbols whose
   `exchangeInfo.orderTypes` lacks `STOP_LOSS` get no silent fallback; the position is flagged unprotected.
2. Every position carries `protection` = `PROTECTED | UNPROTECTED | UNKNOWN | SIMULATED | NONE`. Losing protection
   engages the kill switch (`stop_new_orders`, `by: system`) and the risk check `all_positions_protected` blocks new
   entries. An unknown stop is reconciled by client order id (30 s grace) and never stacked with a second one.
   `protect_position` re-places a stop and is allowed under the kill switch; releasing the switch stays human-only.
3. Exits of a stopped position use `POST /api/v3/order/cancelReplace` with `STOP_ON_FAILURE` (a spot stop locks the
   base asset, so selling first is impossible). Cancel failure → no sell; sell failure after cancel → unprotected,
   kill switch; unknown → nothing resent. The remainder of a partial exit is re-protected.
4. Stop fills are applied by delta of cumulative `executedQty`. `GET /api/v3/order` carries no fills, so their fees
   are estimated from the configured fee rate and labelled `fee_basis: estimated`.

Still open: OTO (entry + stop in one request) would remove the gap between entry fill and stop placement; it needs a
LIMIT working order, so it was not adopted for MARKET entries. Verified only against mocked HTTP; the first Demo
session must exercise each path under supervision.

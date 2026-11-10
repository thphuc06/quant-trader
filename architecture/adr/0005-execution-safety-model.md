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
- Exchange-side protection is a single `STOP_LOSS_LIMIT` sell; targets are not placed on the exchange.

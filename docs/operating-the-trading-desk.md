# Operating the trading desk

For the operator: how to set up, start a Claude Code session, approve actions and handle incidents.
Default state: `PAPER`, live trading locked, risk policy `provisional`.

## 1. Setup (once per machine)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock      # POSIX: .venv/bin/python
.venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m trader doctor --network
```

`.mcp.json` starts the `trading` server through `scripts/run_mcp.py`, which finds `.venv` itself. If your venv lives
elsewhere, set `TRADER_PYTHON` to its interpreter.

Optional `.env` (copy `.env.example`): provider keys for news search, Binance Demo keys. Never paste keys into a chat.

## 2. First Claude Code session in this folder

1. Start `claude` in the repository root and **accept the workspace-trust dialog**. Until you do, the project's
   `permissions.allow` rules are ignored and every read-only tool asks for permission.
2. Approve the `trading` MCP server when asked. `/mcp` must show `trading · connected`.
3. Type `/trading-desk`.

## 3. Roles

| Role | How | Use |
|---|---|---|
| `trader` (default) | nothing to set | analysis and supervised trading; code, configs, policy and skills cannot be edited |
| `builder` | `.claude/settings.local.json` → `{"env": {"TRADER_SESSION_ROLE": "builder"}}`, restart | development |

Switch back to `trader` (delete the file or change the value) when development is done. Human-only commands stay
blocked in both roles.

## 4. What asks for your approval

Read tools run silently. These always ask: `record_decision_report`, `submit_post_trade_review`, `sync_positions`,
`protect_position`, `run_backtest`, `create_trade_intent`, `execute_approved_trade`, `cancel_trade_intent`,
`activate_kill_switch`.

Execution:
- `PAPER`: approving the `execute_approved_trade` prompt is the approval.
- `BINANCE_DEMO` / `LIVE`: in **your own terminal** run `python -m trader approve <intent_id>`, check the order, type
  the symbol. Then let Claude call `execute_approved_trade`. Claude cannot approve for you.

## 5. Protective stops (Demo / Live)

Every exchange position gets a `STOP_LOSS` (stop-market) order. If placing it fails or its state is unknown, the
position is marked `UNPROTECTED`/`UNKNOWN`, the kill switch engages and new entries are blocked.

1. `sync_positions` reconciles the stop with the exchange.
2. `protect_position <symbol>` places a new stop (allowed while the kill switch is on).
3. When everything is `PROTECTED`, release the kill switch yourself: `python -m trader kill-switch release`.

## 6. Incidents

| Situation | Do |
|---|---|
| `/mcp` shows `trading` failed | `python -m trader doctor`; check `.venv` exists; read the server's stderr in `/mcp` |
| Intent in `UNKNOWN` | Do not retry. `get_trade_intent` reconciles by client order id; after 30 s without a match it becomes `FAILED` |
| Position `UNPROTECTED` | Section 5 |
| Want to stop everything | `python -m trader kill-switch engage --reason "..."` (or ask Claude to call `activate_kill_switch`). It blocks new entries; it does not close positions |
| Journal integrity warning | `python -m trader verify-journal`; stop trading until explained |

## 7. Checking the harness itself

`.venv/Scripts/python scripts/harness_eval.py --label <name>` runs six scenarios in fresh `claude -p` sessions
with Haiku (trader role, PAPER, temporary data dir; about $0.05 per run) and writes a summary under
`reports/harness-eval/`. Run it after changing CLAUDE.md, skills, hooks or MCP tools.

## 8. Before Binance Demo

- `python -m trader doctor --network` passes; Demo keys in `.env` (spot trading only, no withdrawals).
- Supervise the first cycle step by step: entry → stop `PROTECTED` → `sync_positions` → exit via SELL intent.
- Live stays locked: it needs `TRADING_MODE=LIVE`, `LIVE_TRADING_ENABLED=true`, live keys, policy `status: approved`
  and an unexpired `python -m trader live-unlock`.

## 9. Forward shadow log (scheduled job)

`python -m trader shadow-log` appends one row per closed 1h bar for each universe symbol: close, all registered
features, rule regime, each baseline's target and validation status, data quality of the feature window, code commit
and `logged_at`. It calls no LLM and places no orders. Rows live in the `shadow_log` table of the operational SQLite
database under `data/`; the table is append-only and a re-run for the same bar adds nothing. A bar the exchange never
published is written as `missing`, never filled. Claude reads it with the read-only tool
`get_shadow_log(symbol, limit)` (max 200 rows).

The first run logs only the latest closed bar (forward data, not a backfill). If the laptop sleeps, the next run
catches up every bar since the last logged one; those rows show a large `lag_seconds_after_close`, so they can be told
apart from on-time rows.

**Register it yourself** (Claude does not create scheduled tasks). From PowerShell or cmd, hourly at 5 minutes past
the hour, output appended to `data\shadow-log.out`:

```
schtasks /Create /TN "ClaudeTrader\ShadowLog" /SC HOURLY /ST 00:05 /F /TR "cmd /c cd /d C:\ClaudeTrader && .venv\Scripts\python.exe -m trader shadow-log >> data\shadow-log.out 2>&1"
```

- Check: `schtasks /Query /TN "ClaudeTrader\ShadowLog" /V /FO LIST` (`Last Result` 0 = all symbols ok, 1 = at least
  one symbol failed; nothing is written for a failed symbol and the next run catches it up).
- Run once now: `schtasks /Run /TN "ClaudeTrader\ShadowLog"`. Remove: `schtasks /Delete /TN "ClaudeTrader\ShadowLog" /F`.
- The task runs only while you are logged on (the default), which needs no stored password. Adjust `C:\ClaudeTrader`
  if the repo lives elsewhere.

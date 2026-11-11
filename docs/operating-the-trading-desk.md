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

**Dust.** Without BNB for fees, Binance takes the entry fee in the base asset, so a full exit leaves a remainder
smaller than one lot step (e.g. 0.00000792 BTC). It can be neither sold nor protected. Such a remainder (below the
step size, `minQty` or `minNotional`) is closed in the ledger as `dust_below_exchange_minimum` at the market price,
with a `dust_written_off` journal event; no order is sent and the kill switch is not engaged. The coins stay in the
exchange balance. Dust left `UNPROTECTED` by code older than 2026-11-11 is cleaned up by `sync_positions` or
`protect_position`; the kill switch it engaged still has to be released by you.

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
- Key type: either a Binance "system generated" HMAC key (`BINANCE_DEMO_API_KEY` + its secret) **or** a
  self-generated Ed25519 key (Binance's recommended type, and the fallback when the portal does not show the HMAC
  secret). For Ed25519, in your own terminal:
  1. `python -m trader keygen --env demo` writes the private key to `data/keys/` (git-ignored; Claude is blocked
     from reading it) and prints the **public** key.
  2. In Binance (Demo) API Management choose *Self-generated*, paste the public key, and copy the API key it shows.
  3. In `.env`: `BINANCE_DEMO_API_KEY=<that key>`, `BINANCE_DEMO_PRIVATE_KEY_FILE=data/keys/binance-demo-ed25519.pem`,
     and no HMAC secret line (both at once is refused as ambiguous).
  Use `--env live` and the `BINANCE_LIVE_*` names for a separate live key; never reuse a demo key.
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

## 10. Funding and open interest (scheduled job)

`python -m trader collect-derivatives` appends Binance USDT-M perpetual funding rates (full history since listing on
the first run) and hourly aggregated open interest to append-only tables. **Binance serves open-interest history for
only about 30 days**, so a gap longer than that is lost for good; the command reports it as
`hours_lost_beyond_retention` and never fills it. The data are research context; nothing here is a tested signal.

Register it as a second task, a few minutes after the shadow log (separate tasks keep separate `Last Result` codes):

```
schtasks /Create /TN "ClaudeTrader\Derivatives" /SC HOURLY /ST 00:07 /F /TR "cmd /c cd /d C:\ClaudeTrader && .venv\Scripts\python.exe -m trader collect-derivatives >> data\derivatives.out 2>&1"
```

`Last Result` 0 = every symbol and both series ok; 1 = at least one part failed (the next run retries it).

## 11. LLM decision evaluation (scheduled job, spends Claude quota)

`scripts/decision_eval.py` starts one fresh headless Claude Code session per universe symbol (`claude -p`, Sonnet,
medium effort) in the **trader role, PAPER mode and a separate data directory** (`data/decision_eval/`). Each session
runs `/analyze-symbol` and records a decision report with a 24h `forecast.probability_up`. Only
`record_decision_report` is allowed beyond the read-only tools; every order tool is denied, so these sessions never
trade and never touch your Demo account or the main database. Forecasts go to
`research/decision-eval/forecasts.jsonl`; raw transcripts to `reports/decision-eval/` (not committed).
`python -m trader decision-eval-score` scores them (no Claude calls). The rules are fixed in
`research/protocols/llm-decision-eval-v1.md`: evaluation once at 180 run-days, harm check from day 60.

Register it once a day at a fixed time (the protocol's primary sample is only these scheduled runs):

```
schtasks /Create /TN "ClaudeTrader\DecisionEval" /SC DAILY /ST 07:10 /F /TR "cmd /c cd /d C:\ClaudeTrader && .venv\Scripts\python.exe scripts\decision_eval.py --sample scheduled >> data\decision-eval.out 2>&1"
```

- `claude` must be on PATH for your user and logged in; the task runs only while you are logged on.
- Each session has a `--max-budget-usd 3` safety cap; four symbols run one after another (roughly 5–15 minutes).
- Ad-hoc runs (`scripts/decision_eval.py` without `--sample scheduled`) are recorded as `manual` and never enter
  the primary sample.
- Your own trader sessions now also record a forecast; those are scored separately as the `operator` sample.

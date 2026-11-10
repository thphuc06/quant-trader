# Prompt for the next builder session

Paste everything below the line into a new Claude Code session started in the repo root with
`TRADER_SESSION_ROLE=builder` (`.claude/settings.local.json`). Report to the operator in Vietnamese.

---

You are continuing work on this repository (Binance Spot long/flat research desk + Claude Code harness). Read
`CLAUDE.md`, `README.md`, `state/plan.md`, `research/research-log.md` and `research/ml-and-validation.md` first.
Do not re-read the whole codebase; open files as needed.

## Where things stand (2026-11-10)

- Harness works: MCP via `scripts/run_mcp.py`, hooks, skills, Haiku eval (`scripts/harness_eval.py`, 6 scenarios).
  Two real trader sessions (Sonnet) analysed BTCUSDT correctly.
- Execution safety for Demo/Live is implemented (STOP_LOSS stops, protection state, cancel-replace exits) but tested
  only against mocks. Live is locked; mode is PAPER.
- No strategy passes the provisional gates. Main finding: costs dominate. 87% of turnover is baseline entry/exit on
  1h bars; modeled costs were about 36% of initial equity over the 2-year development OOS.
- The holdout (from `backtest.holdout_start` = 2025-11-10) is locked. The development OOS 2023-11 → 2025-11 has
  already been viewed many times (65 trials); treat any new result on it as exploratory.

## Your tasks, in order. One commit per task; `pytest`, `ruff check src tests scripts`, `mypy` green before each.

### 0. Housekeeping
- `git status`; push the unpushed commits to `origin main` (no force; fetch first and stop if the remote moved).
- `git stash list`: `stash@{0}` holds only ruff formatting of other files; drop it.

### 1. Forward shadow log (small; start it early because only time produces clean data)
- `python -m trader shadow-log`: for every universe symbol and the decision timeframe, append one row per closed
  bar not yet logged: bar open/close time, close, all registered features, rule regime, each active strategy's
  target, validation status, data-quality status, code commit, and `logged_at` (UTC). No LLM calls, no orders.
- Storage: append-only (SQLite table with UPDATE/DELETE triggers like `journal`, or one Parquet file per symbol per
  month). Idempotent: re-running for the same bar adds nothing. Missing bars are recorded as missing, never filled.
- Read-only MCP tool `get_shadow_log(symbol, limit<=200)` so a trader session can see it.
- Tests: idempotency, no look-ahead (a row for bar t uses only data closed at t), missing-bar handling,
  append-only enforcement.
- Scheduling: write the exact Windows Task Scheduler command (hourly, a few minutes past the hour) into
  `docs/operating-the-trading-desk.md`. **Do not register the task yourself; ask the operator.**

### 2. Research reliability (Step 5 of `state/plan.md`)
a. Portfolio backtester (`src/trader/quant/portfolio.py`): shared capital across the 4 symbols, the risk policy's
   limits (risk per trade, 20% notional cap, 50% total exposure, max 2 positions), position size from the same
   formula as `risk.compute_sizing` (a float mirror is fine for speed if a test proves parity with the Decimal
   version on sample inputs), next-bar-open fills, the existing cost model, stops as in `quant/backtest.py`.
   Deterministic tie-break when more signals than slots (state the rule). Report portfolio-level return, drawdown,
   turnover, costs, exposure.
b. `quant/stats.py`: bootstrap on daily returns, with block-length sensitivity (5, 10, 20 days) reported side by side.
c. Rename the `min_oos_trades` gate to `min_evaluation_trades` and label which period the trades come from.
   Trial accounting for DSR: exclude benchmarks (`buy_and_hold`, `no_trade`), include `ml_model` evaluations.
   Keep old experiment records readable.

### 3. One pre-registered hypothesis: daily-bar baselines to cut turnover
- Write `research/protocols/daily-baselines-v1.md` BEFORE running anything: hypothesis (economic reason: crypto
  time-series momentum evidence is at daily/weekly horizons, Liu & Tsyvinski in `research/source-register.md`),
  rules and parameters fixed a priori (round numbers, e.g. from `features_by_timeframe["1d"]`; no sweep), universe,
  evaluation through the portfolio backtester at 1x and 2x costs, gates, and what each outcome would mean.
  Be explicit that daily bars give few trades, so the trade-count gate may fail, and say what you will conclude then.
- Commit the protocol, then run it **once** on the development period. Record every run, including failures.
  Never touch the holdout.
- Write the result honestly in `research/research-log.md` and the README (negative results included). Do not
  propose paper trading unless every gate passes.

## Rules
- Follow `CLAUDE.md`. Do not weaken safety controls, enable live trading, or edit `configs/risk_policy.yaml`.
- Human-only commands (approve, live-unlock, kill-switch release, lesson promote, `--unlock-holdout`) are off limits.
- If `ruff format` reformats files you did not touch, do not commit those changes.
- Stop and ask the operator only for: registering the scheduled task, anything that costs money or needs keys, or a
  result that would change the plan.
- At the end: update `state/plan.md` and `state/session-summary.md`, push only if the operator says so, and give a
  short Vietnamese summary: commits, test count, the hypothesis result with its limitations, and what needs the
  operator.

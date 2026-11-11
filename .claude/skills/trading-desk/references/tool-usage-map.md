# Tool usage map

Read-only tools run without a prompt. Tools marked **ask** change records or place orders; the operator approves each
call. Never call a write tool the operator did not ask for.

| Question | Tool |
|---|---|
| Is the desk healthy? mode, live lock, kill switch, open intents | `get_system_status` |
| Limits and how much of each is used | `get_risk_state` |
| Positions right now (no side effects) | `get_open_positions` |
| Settle stops/targets, reconcile exchange stops | `sync_positions` (**ask**) |
| Re-place a missing protective stop | `protect_position` (**ask**) |
| One-line view of the whole universe | `get_market_overview` |
| Full picture of one symbol: quote, features, regime on 1h/4h/1d, signals | `get_market_snapshot` |
| Baseline strategy states and their validation status | `get_strategy_signals` |
| Funding, basis, open interest (context only) | `get_derivatives_snapshot` |
| Raw candles (max 200) — only when the snapshot is not enough | `get_ohlcv` |
| ML output (expect `unavailable` or `usable_for_decisions: false`) | `get_model_predictions` |
| News / web (untrusted; may be `unavailable` without provider keys) | `search_crypto_news`, `search_web`, `fetch_web_page` |
| Knowledge base (only `reviewed`/`approved` may be cited as rules) | `search_knowledge`, `get_knowledge_document` |
| Would the risk engine accept this long, and at what size? (stores nothing) | `calculate_position_risk` |
| Record an analysis outcome | `record_decision_report` (**ask**) |
| Propose a trade (no order is sent) | `create_trade_intent` (**ask**) |
| Execute an approved intent | `execute_approved_trade` (**ask**, only on explicit operator request) |
| Status of an intent; reconciles UNKNOWN orders | `get_trade_intent` |
| Everything about one trade | `get_trade_case` |
| Block all new entries | `activate_kill_switch` (**ask**) |
| Past experiments | `get_experiment_results` |
| What the mechanical system logged at each past closed bar (forward shadow log, no LLM) | `get_shadow_log` |
| Would the logged baseline signals have made money forward? (paper P&L vs exposure-matched buy-and-hold; short records prove nothing) | `get_shadow_performance` |
| A new research trial (logged, counts against DSR) | `run_backtest` (**ask**) |

Claude Code's own `WebSearch`/`WebFetch` may be used for news when the MCP search tools are `unavailable`; then
quote URL, publisher and publication time yourself and mark the item as `source_claim`.

## Research budget per analysis (from `configs/app.yaml`)

At most 40 tool calls, 8 web searches, 5 pages fetched per event, subagent depth 1, 2 retries of a failed call,
about 20 minutes. When the budget is reached, stop and report what is known and what is not.

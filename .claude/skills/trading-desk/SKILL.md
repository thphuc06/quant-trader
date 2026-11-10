---
name: trading-desk
description: Initialize a supervised trading session and review system, data, policy and open-intent state.
---

1. Call `get_system_status`, `get_risk_state`, and `get_open_positions`.
2. Confirm mode is PAPER unless the operator explicitly states otherwise; never change the mode.
3. Report stale/degraded data, provisional policy, open positions, unresolved intents and kill-switch state.
4. Summarize the universe, active strategies and validation status. No strategy currently passes the research gates.
5. Ask what symbol or research task the operator wants to inspect. Do not create an intent during initialization.

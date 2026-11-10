---
name: analyze-symbol
description: Analyze one configured spot symbol using current evidence and deterministic backend calculations.
---

1. Call `get_market_snapshot` and `get_strategy_signals` for the requested configured symbol.
2. Check timestamps, data-quality warnings, regime, signals, exposure and risk context.
3. Use `search_knowledge` only to retrieve relevant candidate guidance; label candidate material as unapproved.
4. Separate observed data from interpretation. Cite every evidence ID and state uncertainty.
5. Return `NO_TRADE` or `WATCH` if the data is degraded or no validated edge exists. Never invent expected return.
6. If the operator asks to pursue a trade, present a thesis and risks, then require permission for any intent tool call.

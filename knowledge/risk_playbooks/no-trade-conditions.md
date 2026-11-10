---
id: kb-risk-no-trade-conditions
title: When the correct decision is NO_TRADE or WATCH
type: risk_playbook
status: candidate
tags: [risk, no-trade, process]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: medium
applicability: every analysis
sources: []
limitations: A process rule set written before any trading history exists. Revise from reviewed trade cases.
related: []
---

`NO_TRADE` is a result, not a failure to produce one. Record it with `record_decision_report` and a reason.

## Hard stops (do not create an intent)

| Condition | How it shows up |
|---|---|
| Any tool needed for the decision returned `stale`, `unavailable` or `error` | envelope `status` |
| Kill switch engaged, risk policy unreadable, or an intent in `UNKNOWN` | `get_system_status`, `get_risk_state` |
| Daily/weekly loss limit or drawdown limit reached | `get_risk_state.usage` |
| `calculate_position_risk` reports failed checks | `failed_checks` |
| The only supporting evidence is unvalidated: an unvalidated strategy signal, an unvalidated model, or a `candidate` document | `validation.status`, `usable_for_decisions`, `citable_as_rule` |

## Soft stops (prefer `WATCH`; a trade needs an explicit argument for why it is still reasonable)

- Regime is `range` or `unknown` on the decision timeframe, or the 1h and 4h regimes disagree.
- A scheduled event that could move the market falls inside the holding horizon and you could not establish its timing from a primary source.
- A material news claim could not be corroborated (one primary source, or two independent reputable ones).
- Spread or realized volatility is far above its recent level (`rv_pct` near 1).
- The stop that makes technical sense is so far away that the notional cap, not the risk budget, would bind — the trade is then mostly gap risk.
- You already hold a position in another universe symbol: the four symbols are highly correlated, so a second position is mostly the same bet.

## Today's default

No strategy in this repository has passed its validation gates and no model is validated. Until that changes, analyses
should normally end in `NO_TRADE` or `WATCH`, and any paper trade is an exercise of the process, not a claim of edge.
Say so plainly when asked for a trade.

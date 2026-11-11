---
id: kb-note-research-status
title: What has been tested and what it means for decisions (research status)
type: note
status: candidate
tags: [research, validation, baseline, no-trade]
created: 2026-11-11
updated: 2026-11-11
author: Claude (Opus 5.5), builder session; summary of research/research-log.md
confidence: medium
applicability: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT spot; all timeframes the desk uses
sources:
  - {title: "research/research-log.md", url: "research/research-log.md", published: 2026-11-11, accessed: 2026-11-11}
  - {title: "research/validation-framework-v2.md", url: "research/validation-framework-v2.md", published: 2026-11-11, accessed: 2026-11-11}
limitations: A summary of the desk's own backtests on four hindsight-chosen large caps with assumed costs. It describes evidence, not markets. Superseded by any later entry in the research log.
related: [exp_28ca944d212649438778, exp_64ca1d365668427eaa1e, exp_4a288b0422184f5c96d4, exp_f37a418ccc024970b198]
---

## Bottom line for an analysis

No strategy, indicator or model on this desk has evidence of an edge after costs. The honest default is `NO_TRADE` or
`WATCH`. Indicators and baseline signals describe the market; none of them is a validated reason to trade.

## What was tested

| Idea | Result | Meaning |
|---|---|---|
| Two rule baselines on 1h bars (`tsmom_trend_v1`, `donchian_breakout_v1`) | No Sharpe interval above zero; losses after costs on several symbols; costs ≈ 36% of equity over 2 years | Hourly trading is eaten by costs |
| Volatility-scaled exposure, 5pp band, daily resizing | Did not cut turnover materially; no full gate pass | Sizing tweaks do not fix hourly costs |
| ML (logistic, gradient boosting) on the registered features | Log loss and Brier worse than base rate, AUC ≈ 0.50 | No predictive signal in those features |
| Same rules on daily bars, 2023–2025 | Turnover cut ≈ 95%, still positive at 2× costs, but too few trades and the Sharpe interval includes zero | Inconclusive |
| Same daily rules on untouched 2017–2021 data (framework v2) | Positive returns, but timing skill vs exposure-matched buy-and-hold not demonstrated (p 0.17–0.22) | Rules dropped. Weak test, confounded by the drawdown lock: "not demonstrated", not "no edge" |

## How to use this in a decision report

- Cite it as context (`candidate`, not a rule) when explaining why a baseline signal is not a reason to act.
- A strategy signal showing `long` means the rule's condition holds, nothing more.
- Model predictions are uninformative (`usable_for_decisions: false`).
- What would change this: a strategy reaching VALIDATED under `research/validation-framework-v2.md`, recorded as an
  experiment, and reviewed by the operator.

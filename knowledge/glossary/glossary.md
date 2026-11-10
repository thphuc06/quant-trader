---
id: kb-glossary
title: Glossary
type: glossary
status: candidate
tags: [glossary]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: high
applicability: all
sources: []
limitations: none
related: []
---

- **bps** — basis point, 0.01%.
- **Decision report** — the structured record of an analysis (`record_decision_report`). Required before any intent.
- **DSR (Deflated Sharpe Ratio)** — probability that the true Sharpe is above what the best of N unskilled trials would show. Falls as more things are tried.
- **Embargo / purge** — dropping training samples whose label window overlaps the test period.
- **Evidence ID** — identifier returned with every MCP tool result (`ev_…`). A decision may only cite IDs the server issued.
- **Holdout** — data at or after `backtest.holdout_start`, excluded from development. Looking at it spends it.
- **Intent** — a proposed trade awaiting validation and approval. Not an order.
- **MAE / MFE** — maximum adverse / favourable excursion of price during a trade, relative to entry.
- **PBO** — probability of backtest overfitting: how often the in-sample best configuration is below median out of sample.
- **PSR** — probabilistic Sharpe ratio: probability the true Sharpe exceeds a benchmark given sample length and return shape.
- **R** — one unit of initial risk: entry minus stop.
- **Regime** — rule-based description of trend and volatility state (`rule_regime_v1`).
- **Trial** — any backtest or model configuration that was evaluated. All are logged.
- **Triple barrier** — label defined by which is hit first: an upper price barrier, a lower one, or a time limit.
- **Walk-forward** — train on a window, test on the next block, roll forward.

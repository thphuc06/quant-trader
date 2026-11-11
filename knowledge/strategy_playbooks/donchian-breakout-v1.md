---
id: kb-playbook-donchian-breakout-v1
title: Donchian breakout baseline (donchian_breakout_v1)
type: playbook
status: candidate
tags: [breakout, volatility, baseline, 1h, long-flat]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: low
applicability: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT spot; 1h bars; transitions from low to high volatility
sources: []
limitations: No external evidence was collected for this exact rule. It is a classic channel-breakout construction used here as a second, differently-shaped baseline.
related: []
---

## Research status (2026-11-11) — read first

**Not validated. A long signal from this rule is not a reason to trade.**

- 1h version (this card): no configuration had a Sharpe confidence interval above zero; PBO for its parameter family
  was 0.45 (high overfitting risk when choosing parameters); turnover ≈ 56–63× notional per year; its portfolio
  backtest under the current risk policy tripped the 10% drawdown floor (`exp_4af2060a`).
- Daily version (55/20): inconclusive on 2023–2025 (`exp_f37a418c`); on untouched 2017–2021 data **timing skill was
  not demonstrated** (p = 0.17 vs exposure-matched buy-and-hold, `exp_64ca1d36`) and the rule was dropped under
  framework v2. Weak, confounded test: "not demonstrated", not "proven to have no edge".
- Details: `research/research-log.md`, `research/validation-framework-v2.md`.

## Hypothesis

Volatility clusters. A close above the highest high of the last four days often coincides with new information and the
start of a higher-volatility leg; staying long until a two-day low is broken keeps exposure to that leg.

## Rules (code: `src/trader/quant/strategies.py`)

- Enter long when the 1h close is above the highest high of the previous 96 bars (excluding the current bar).
- Exit when the close is below the lowest low of the previous 48 bars.
- Stop at entry − 3 × ATR(14), fixed at entry.
- Parameters are round numbers (4 days / 2 days), not fitted.

## When it should fail

- False breakouts in ranges; entries happen exactly when spread and slippage are worst.
- Overlap with the trend baseline: both are long during the same up-trends, so they are not two independent bets.

## What to check before using a signal

Same checklist as `kb-playbook-tsmom-trend-v1`. Additionally compare `rel_volume` and `rv_ratio` in the snapshot: a
breakout on below-average volume with falling volatility contradicts the hypothesis (an interpretation, not a tested rule).

## Validation status

See `research/experiments/` and `research/research-log.md`. `candidate` until the gates are passed.

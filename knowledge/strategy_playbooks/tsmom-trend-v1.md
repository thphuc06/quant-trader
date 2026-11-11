---
id: kb-playbook-tsmom-trend-v1
title: Time-series momentum trend baseline (tsmom_trend_v1)
type: playbook
status: candidate
tags: [trend, momentum, baseline, 1h, long-flat]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: low
applicability: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT spot; 1h bars; trending regimes
sources:
  - {title: "Liu & Tsyvinski, Risks and Returns of Cryptocurrency (RFS 2021)", url: "https://www.nber.org/papers/w24877", published: 2018, accessed: 2026-11-10}
limitations: The cited evidence is daily/weekly momentum on a 2011-2018 sample without our costs. It does not establish anything about 1h bars after 2018.
related: []
---

## Research status (2026-11-11) — read first

**Not validated. A long signal from this rule is not a reason to trade.**

- 1h version (this card): after costs it lost money on 3 of 4 symbols over the development period; no configuration
  had a Sharpe confidence interval above zero; about 87% of turnover was its own entries/exits and modeled costs were
  ≈ 36% of equity over two years. Under the current risk policy its portfolio backtest also tripped the 10% drawdown
  floor (`exp_0851cd06`).
- Daily version (EMA 20/100, 90-day momentum): inconclusive on 2023–2025 (`exp_4a288b04`); on untouched 2017–2021 data
  **timing skill was not demonstrated** (p = 0.22 vs exposure-matched buy-and-hold, `exp_28ca944d`) and the rule was
  dropped under framework v2. The test was weak and confounded by the drawdown lock, so this is "not demonstrated",
  not "proven to have no edge".
- Details: `research/research-log.md`, `research/validation-framework-v2.md`.

## Hypothesis

Returns show positive autocorrelation over horizons of days to weeks because information and attention spread slowly.
A position held while both a slow trend filter and a medium-horizon return are positive should capture part of it.

## Rules (code: `src/trader/quant/strategies.py`)

- Long when `EMA(24) > EMA(168)` **and** the 168-bar log return is positive. Flat otherwise.
- Decided on the close of a 1h bar, executed at the next bar's open.
- Stop at entry − 3 × ATR(14), fixed at entry. After a stop-out, no re-entry until the signal has gone flat once.
- Parameters are round numbers in hours (1 day, 1 week). They were **not** fitted.

## When it should fail

- Range regimes: repeated small losses (whipsaw) plus fees.
- Sharp reversals: the exit signal lags; the ATR stop is the only fast exit and gaps fill worse than the stop.
- If the effect has been arbitraged away since the cited sample.

## What to check before using a signal

1. `get_strategy_signals` → `validation.status`. If `not_validated`, the signal is context, not a reason to trade.
2. Regime on 1h and 4h (`get_market_snapshot`). The hypothesis only applies to `trend_up`.
3. Latest experiment for the symbol (`get_experiment_results`): sample size, Sharpe confidence interval, DSR, result at 2× costs.

## Validation status

See `research/experiments/` and `research/research-log.md`. Until the provisional gates in `configs/app.yaml`
are passed, this card stays `candidate` and must not be cited as a rule.

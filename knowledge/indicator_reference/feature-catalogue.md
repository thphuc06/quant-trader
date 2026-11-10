---
id: kb-indicator-feature-catalogue
title: Feature catalogue — what each feature measures and what it duplicates
type: indicator_reference
status: candidate
tags: [features, indicators, regime]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: medium
applicability: all symbols and timeframes served by get_market_snapshot
sources: []
limitations: Descriptions of standard constructions. None of these features has shown predictive value in this repository's own tests yet.
related: []
---

Exact formulas, lookbacks and units are served by the resource `trading://strategies/active` (generated from
`src/trader/quant/features.py`). This note is about interpretation.

| Feature | Measures | Reading it | Overlaps with |
|---|---|---|---|
| `ema_spread` | Direction and size of the gap between a fast and a slow average | > 0: recent prices above the longer average | `tsmom` (strongly) |
| `tsmom` | Return over the lookback | The raw quantity trend strategies use | `ema_spread` |
| `adx` | Strength of directional movement, not direction | < 20: no trend by the rule regime's convention | — |
| `rsi` | Balance of recent gains vs losses | Extremes describe the last ~14 bars; they are not reversal signals by themselves | short-horizon `tsmom` |
| `atr`, `atr_pct` | Typical bar range, absolute and relative | Used for stop distance; compare a stop with ATR, not with a fixed percentage | `rv` |
| `rv` | Annualized realized volatility over the short window | Level of recent volatility | `atr_pct` |
| `rv_ratio` | Short-window vs long-window volatility | > 1: volatility expanding | — |
| `rv_pct` | Where current volatility sits in the trailing distribution (0–1) | Drives the `vol_low / vol_normal / vol_high` label | — |
| `donchian_pos` | Location of the close inside the recent high–low range (0–1) | Near 1: at the top of the range; a breakout needs a close above it | — |
| `rel_volume` | Volume relative to its trailing average | < 1: below-average participation | — |
| `taker_buy_ratio` | Share of volume initiated by buyers | A coarse flow proxy from candle data; noisy | — |
| `vwap_dev` | Distance of the close from the trailing 24-bar VWAP | Stretch relative to where volume traded | short-horizon `tsmom` |

## Regime label (`rule_regime_v1`)

`trend_up` / `trend_down` need ADX ≥ 20 and the sign of `ema_spread`; otherwise `range`. Volatility state comes from
`rv_pct` thirds. Thresholds are conventions, not fitted values. The label is a description of the recent past; it does
not forecast the next regime.

## Using features as evidence

- A feature value is an **observed fact** (cite the snapshot's `evidence_id`). What it implies is **interpretation**.
- Two overlapping features are one piece of evidence, not two.
- Warm-up values are `null`. A `null` is "not available", never zero.

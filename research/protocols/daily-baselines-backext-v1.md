# Protocol H1: frozen daily baselines on untouched 2017–2021 data

Status: **PREREGISTERED 2026-11-11**, committed before any 2017–2021 candle was downloaded. Judged under
[validation-framework-v2.md](../validation-framework-v2.md). One run; every run, including failures, is recorded.
Origin: hypothesis H1 in [reviews/2026-11-11-low-frequency-evaluation.md](../reviews/2026-11-11-low-frequency-evaluation.md).

## Question

Do the two daily rules from [daily-baselines-v1](daily-baselines-v1.md), unchanged, show timing skill on Binance data
that no registered trial has touched? Every earlier trial used data from 2021-11-11 onward.

## Frozen specification

- **Rules (no change from v1):** `tsmom_trend_v1` with EMA 20/100 and 90-day log momentum; `donchian_breakout_v1`
  with entry 55 / exit 20; stop entry − 3 × ATR(14) of the signal bar, clipped at the policy's 10% maximum distance.
- **Portfolio:** `quant/portfolio.py`, risk policy v0.1.0 limits as committed, next-bar-open fills, tie-break
  BTCUSDT > ETHUSDT (the slot limit cannot bind with two symbols).
- **Universe:** BTCUSDT and ETHUSDT only. SOLUSDT did not exist; BNBUSDT is left out to keep the universe fixed in
  advance rather than chosen by listing date.
- **Data:** Binance Spot 1h klines from the first available bar (expected August 2017) to 2021-11-11 00:00 UTC
  exclusive, downloaded by the builder into a separate store (`data/candles_pre2021/`) so the main store and all earlier
  runs are unchanged. Daily bars = complete UTC days aggregated from 1h bars (as in v1); incomplete days are dropped,
  never filled; gaps are listed in the record. Native 1d klines for the same period are downloaded only to check the
  aggregation.
- **Evaluation window:** 2017-12-01 00:00 UTC → 2021-11-11 00:00 UTC (≈ 3.95 years). Earlier bars are warm-up
  (EMA 100 and 90-day momentum need ≈ 100 days). Most of the 2017 rally is therefore outside the window.
- **Costs:** 13 bps per side base, 26 bps stress. **2× (26 bps) is the primary cost level** for every gate, because
  spreads and slippage in 2017–2018 are unverified.
- **Configurations:** 2. Binding DSR N = 2 (untouched sample, protocol committed first); DSR with the raw registry
  count + 2 is reported beside it.
- **Gates and tiers:** framework v2 exactly (G-timing with K = 2,000 shifts, seed 7, margin 30 days; G-cost; G-ci
  with 5/10/20-day blocks; G-dsr; G-span ≥ 30 trades). Implied G-ci threshold for ≈ 3.95 years: observed annual
  Sharpe ≈ 0.9.

## What each outcome decides (per rule)

| Outcome on this sample | Decision |
|---|---|
| REJECTED (G-timing or G-cost fails) | The rule is dropped: no further trials of it on any data, not added to the shadow log. If both are rejected, the daily-trend direction ends for this desk |
| INCONCLUSIVE | Added to the shadow log as a frozen rule with a registered start date; framework v2 exit rules apply |
| VALIDATED | Not sufficient on its own (different era, two coins). Treated as INCONCLUSIVE for action: shadow log, then a pooled pre-registered evaluation (this sample + forward data) is the only route to a paper proposal |

No threshold, window, universe or parameter will be changed after the data are downloaded.

## Expectations stated in advance

- ≈ 20–40 closed trades per rule: G-span will probably fail, so INCONCLUSIVE is the best likely outcome.
- Minimum detectable annual Sharpe with 80% power at this length ≈ 1.1.
- The window contains two large bull markets; G-timing (exposure-matched benchmark) is the gate that separates
  timing from simply being long.

## Limitations

The parameters are classic, widely known values and 2017–2021 is a well-known bull era, so the sample is untouched by
this desk's trials but not by the field. USDT left its peg at times. Reported volume in this era is unreliable (not
used). Exchange outages appear as missing days. Two coins only.

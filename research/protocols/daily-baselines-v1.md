# Protocol: daily-bar baselines through the risk-policy portfolio

Status: preregistered 2026-11-11, before any code for this variant was written and before any run. One run on the
development period; every run, including failures, is recorded in `research/experiments/`. The holdout stays locked.

## Why

Costs dominate the current research. Turnover attribution showed that 87% of turnover on 1h bars is baseline
entry/exit, and modeled costs were about 36% of initial equity over the two-year development OOS. Neither a 5pp
no-trade band nor daily volatility resizing cut turnover by more than 3%, because they did not touch the signal clock.

The economic case for a slower clock: the crypto time-series momentum evidence is at daily and weekly horizons
(Liu & Tsyvinski 2021, `LT` in [source-register.md](../source-register.md); sample ends 2018, no costs at our
frequency, may have decayed). A trend rule that decides once per day should change state far less often than the
same rule on hourly bars, so it should pay a fraction of the costs, while keeping whatever trend exposure exists at the
horizon the literature describes.

## Hypothesis

H1 (mechanism, primary): running the two baselines on UTC-daily bars through the portfolio backtester at least halves
annual portfolio turnover compared with the same baselines on 1h bars through the same backtester, over the same
window, at base costs.

H2 (economic, secondary): at least one daily candidate passes all provisional validation gates below.

## Frozen specification

**Data.** Stored Binance Spot 1h candles (`trader ingest`), BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT. Daily bars are
aggregated from them by UTC day: open = first open, high = max, low = min, close = last close, volumes summed. A day
with fewer than 24 hourly bars is not built (treated as a missing daily bar; nothing is filled). Native 1d klines are
not used because the store has them only from 2025-06 for BTC/ETH; where they overlap the development period, the
aggregation is compared with them as a data check. Bars at or after the holdout start are dropped before any
computation.

**Window.** Evaluation from `2023-11-12T00:00Z` (first full UTC day of the development OOS used by earlier trials)
to `2025-11-10T00:00Z` exclusive (holdout start). Earlier bars are warm-up only.

**Candidates (2).** Existing code, parameters fixed a priori as round numbers; no sweep:

| Candidate | Rule | Parameters |
|---|---|---|
| `tsmom_trend_v1` daily | long when EMA(fast) > EMA(slow) and log momentum > 0 | `ema_fast` 20, `ema_slow` 100, `mom_lookback` 90 (from `features_by_timeframe["1d"]`), `atr_stop_mult` 3.0 |
| `donchian_breakout_v1` daily | long on close above prior 55-day high; flat on close below prior 20-day low | `entry_lookback` 55 (`features_by_timeframe["1d"].donchian_lookback`), `exit_lookback` 20 (the conventional 55/20 breakout pair), `atr_stop_mult` 3.0 |

**Stops.** Entry − 3 × ATR(14) of the signal bar, as for the hourly baselines. Daily 3×ATR can exceed the policy's
10% maximum stop distance; the stop is then placed at exactly 10% below entry (`clip_stop_to_policy`) rather than the
entry being skipped. Stops below the 0.3% minimum are rejected as in the risk engine.

**Portfolio.** `quant/portfolio.py`, shared capital, risk policy v0.1.0 as committed (0.5% risk per trade, 20%
notional cap, 50% total exposure, max 2 positions, stop-distance band, daily/7-day loss limits, 10% drawdown floor,
60-minute loss cooldown). Next-bar-open fills. Tie-break: fixed universe order BTC > ETH > SOL > BNB.

**Costs.** 13 bps per side (fee 10, half-spread 1, slippage 2); stress 2× = 26 bps.

**Reference (not a trial).** The same two strategies with their registered hourly parameters, same portfolio
backtester, window, costs and stop rule, on 1h bars. Recorded with `role: reference`, excluded from the DSR count.
These rules were already trials; this run only provides the turnover comparator.

## Gates (provisional values from `configs/app.yaml`, unchanged)

| # | Gate | Rule |
|---|---|---|
| H1 | Turnover cut | candidate turnover/year ≤ 50% of its hourly reference |
| G1 | Enough trades | ≥ 100 closed trades in the evaluation window, all four symbols combined |
| G2 | Sharpe CI above zero | lowest 90% CI lower bound of annualized daily Sharpe across 5/10/20-day block bootstraps > 0 |
| G3 | Selection-robust | DSR ≥ 0.5 on daily returns, trial count = all counted registry trials + these 2 candidates, Sharpe variance with trials rescaled to daily |
| G4 | Cost-robust | net total return > 0 at 2× costs |

Paper trading may be proposed only if a candidate passes H1 and G1–G4. Reported in addition (not gates): net return,
max drawdown, exposure, costs in bps of initial equity, trades by symbol, binding sizing constraints, entry
rejections, and bootstrap total-return intervals.

## What each outcome means

- **H1 and G1–G4 pass:** evidence is still exploratory (this development OOS has been viewed by 65 earlier trials).
  Propose to the operator: forward shadow observation first, then a paper test. No holdout unlock, no live/demo.
- **H1 passes, some of G2–G4 fail:** the slower clock fixes the cost mechanics but shows no edge after costs.
  `NO_TRADE` / `WATCH` stays. No parameter changes on this period.
- **G1 fails (expected).** Daily trend rules change state a few times per symbol per year and at most two positions
  can be open, so 100 trades in two years is unlikely. Then the Sharpe/DSR results are too imprecise to rank anything,
  whatever their sign. Conclusion in that case: inconclusive and not a paper candidate. We will not lower the
  threshold, add symbols or extend the window after seeing the result; more evidence can only come from forward data
  (for example adding the daily rules to the shadow log, which needs the operator's agreement).
- **H1 fails:** daily bars do not reduce turnover under this policy; drop the direction.

## Limitations stated in advance

Universe chosen with hindsight; costs are assumptions; no partial fills, latency or impact; intrabar stop order
unknown; daily aggregation skips incomplete days, so row-based lookbacks span an extra calendar day around a gap;
positions are not resized after entry, so exposure drifts with prices; the tie-break favours earlier symbols; the
development OOS has been viewed many times, so no result here is confirmatory.

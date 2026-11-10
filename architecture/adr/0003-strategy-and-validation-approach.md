# ADR 0003 — Two rule baselines, custom simulator, trial accounting

Status: accepted · 2026-11-10

## Decision
- Baselines: `tsmom_trend_v1`, `donchian_breakout_v1`; benchmarks `buy_and_hold`, `no_trade`.
- ML: logistic regression and gradient boosting on triple-barrier labels, purged walk-forward, calibrated. Built, but
  `ml.use_for_decisions: false`.
- Backtests: a small custom bar simulator (next-open fills, intrabar stops, proportional costs) instead of
  Freqtrade/VectorBT/Backtrader.
- Every run is appended to an experiment registry (failures included). The last 365 days are a locked holdout.
- Statistics: bootstrap CIs, PSR, DSR with the registry's trial count, PBO/CSCV when a configuration family exists.
- All acceptance thresholds are provisional and live in config.

## Why
Transparency and shared code between backtest and live signal generation matter more here than simulation speed or
exchange plumbing. See [research/ml-and-validation.md](../../research/ml-and-validation.md).

## Consequences
- We own simulator bugs → it is covered by hand-computed unit tests.
- No order-book realism, partial fills or latency: acceptable only for small size at hourly frequency in the deepest books.
- Until a strategy clears the gates, the desk's honest default output is `NO_TRADE` / `WATCH`.
- Passing the gates justifies paper trading only.

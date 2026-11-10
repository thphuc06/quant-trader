# ML methods, backtest validity, metrics

Labels and source IDs: see [README.md](README.md) and [source-register.md](source-register.md).

## 1. What to predict

Direction ("up or down") ignores costs and path. We frame a **cost-aware barrier problem** (AFML, background):

- For each closed bar *t*, set an upper barrier at `close + k_up·ATR`, a lower barrier at `close − k_dn·ATR` and a time
  limit of *h* bars. Label = 1 if the upper barrier is touched first, else 0 (lower barrier or timeout).
- If one bar touches both barriers, the lower one is assumed first (pessimistic; intrabar order is unknowable from OHLC).
- Labels at *t* use bars *t+1 … t+h*, so labels **overlap**. That is why random K-fold is invalid here and purging is needed (§3).

Barrier widths are in ATR units so the label is comparable across volatility regimes. `k` and `h` are research
parameters; every value tried counts as a trial (§4).

## 2. Model ladder

Complexity is added only when the previous rung is beaten out of sample, net of costs.

| Rung | Method | Status |
|---|---|---|
| 0 | Base rate (predict the training prevalence) | Implemented — the reference for log loss / Brier |
| 1 | Rule baselines (`tsmom_trend_v1`, `donchian_breakout_v1`) | Implemented |
| 2 | Logistic regression on standardized features | Implemented (`logreg_v1`) |
| 3 | Gradient boosting (`HistGradientBoostingClassifier`) | Implemented as challenger (`hgb_v1`). Chosen over LightGBM/XGBoost/CatBoost only to avoid another dependency; on ~30k rows and ~10 features the libraries are not expected to differ materially (hypothesis) |
| — | Random forest | Skipped: dominated by rung 3 for tabular data of this size (background) |
| — | HMM / change-point regimes | Deferred. A rule regime (trend × volatility percentile) is transparent and has no fitted state to drift |
| — | TCN / LSTM / Transformer | Rejected for now: tens of thousands of highly autocorrelated bars cannot support them, and no simple model has shown signal yet |
| — | Meta-labelling, ensembling | Deferred until a primary signal survives validation |

**Calibration.** Raw scores are not probabilities. The pipeline fits the model on the early part of each training
window and a sigmoid (Platt) calibrator on the later part, with a purge gap between them. Isotonic is available but
needs more data to avoid step artefacts. A model's output carries `calibration_status`; `p = 0.70` must not be read as
"70% chance" unless that status is `evaluated` and the reliability table agrees.

**Explanations.** Permutation importance on out-of-sample folds only. SHAP is deferred; either way importance describes
the model, not the market, and is unstable for collinear features.

## 3. Validation design

- **Chronological only.** No shuffling anywhere.
- **Walk-forward** (rolling origin): train on a window, test on the next block, roll.
- **Purging + embargo**: any training sample whose label window overlaps the test block is dropped, plus an embargo of
  *h* bars after each test block (AFML, background).
- **Final holdout**: the most recent 365 days are excluded from all development runs. The CLI refuses to touch them
  without `--unlock-holdout`, and each unlock is written to the experiment log. A holdout that has been looked at more
  than once is no longer a holdout.
- **Trial accounting**: every backtest/model run appends a record to the experiment registry — failures included — so
  the number of trials is known when DSR is computed. Since 2026-11-11 the DSR count (`ExperimentRegistry.trials`)
  excludes the benchmarks `buy_and_hold` / `no_trade` and diagnostic records, and includes every ML model evaluation
  (which adds to N but has no Sharpe for the variance) and every portfolio candidate. Sharpe ratios from different bar
  intervals are rescaled to the candidate's interval (× √(bars per year ratio)) before their variance is taken.
- **Daily bootstrap**: portfolio results are also judged on UTC-day returns with a circular block bootstrap at 5, 10
  and 20-day blocks reported side by side (`stats.daily_bootstrap_sensitivity`); a gate uses the lowest lower bound.
- **PSR / DSR** (DSR, literature): implemented in `src/trader/quant/stats.py`. DSR needs the number of trials and the
  variance of their Sharpe ratios; correlated trials make the effective N smaller than the count, so we report DSR with
  the raw count as a conservative bound and say so.
- **PBO via CSCV** (PBO, literature): implemented; only meaningful when a *family* of configurations was compared (it
  needs a T×N matrix of per-period returns for N ≥ ~10 configurations). Otherwise reported as `not_applicable`.
- **Block bootstrap** (circular) for confidence intervals on Sharpe and total return, because hourly returns are
  serially dependent.
- **Ablations** to run before any strategy is called validated: without regime filter; without ML; without news veto;
  without Claude (pure rule execution). Claude's incremental contribution can only be measured in paper trading, by
  comparing its decisions with the mechanical signal log.

### Bias checklist (each has a test or an explicit limitation)

| Bias | Control |
|---|---|
| Look-ahead in features | Truncation test: features at *t* identical with and without future rows |
| Look-ahead in execution | Signal on close of *t*, fill at open of *t+1* |
| Recursive-indicator instability | Warm-up rows dropped; EMA/ATR values compared across different history lengths |
| Unclosed candles | Collector and REST ingestion drop any candle whose `close_time` is in the future |
| Label leakage | Purge + embargo |
| Survivorship / universe selection | **Not solved** — fixed large-cap universe chosen with hindsight; stated in every report |
| Intrabar ambiguity | Stop assumed hit before target when both fall inside one bar; gaps fill at the open, not at the stop |
| Selection bias across trials | Trial log + DSR/PBO |
| Backtest/live parity | The same feature and signal functions serve backtest and the MCP tools |

## 4. Cost model

| Component | Default (provisional) | Basis |
|---|---|---|
| Fee | 10 bps per side (taker) | FEE-SQ (low–medium); replace with the account's `commissionRates` |
| Half-spread | 1 bp | hypothesis for BTC/ETH; measured live by `get_market_snapshot` |
| Slippage | 2 bps per side | hypothesis; to be replaced by measured paper/demo slippage |
| Funding / borrow | 0 | Spot, no margin |
| Latency, partial fills, queue position | not modelled | Acceptable only because we trade small size at next-bar open in the deepest books; stated as a limitation |

Every report prints the cost assumptions next to the result. A sensitivity run at 2× costs is part of the standard report.

## 5. Backtest engine choice

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Freqtrade | Full bot, exchange plumbing, hyperopt | A whole runtime with its own strategy/risk model — duplicates and conflicts with our risk engine and MCP boundary; hyperopt invites overfitting | Rejected |
| VectorBT | Very fast parameter sweeps | Speed is not our constraint; fast sweeps make trial inflation easy; path-dependent stops are awkward in the open-source version | Rejected |
| Backtrader | Mature event engine | Unmaintained for years (background); heavy API for a long/flat 1h rule | Rejected |
| **Custom bar simulator** | ~200 lines, every assumption visible and unit-tested, shares feature/signal code with the live path | We own the bugs; no order-book realism | **Chosen** |

The simulator is deliberately simple: one symbol at a time, long/flat, next-open fills, intrabar stops, proportional
costs. It is a measuring instrument for coarse hypotheses, not a market simulator.

`quant/portfolio.py` (2026-11-11) runs several symbols on one shared account under the risk policy: entry size from a
float mirror of `risk.compute_sizing` (parity tested), 20% notional cap, 50% total exposure, at most 2 positions, stop
distance band, daily/7-day loss limits, drawdown floor and loss cooldown; same fills, stops and cost model as the
single-symbol simulator. When more symbols signal than slots allow, the fixed universe order (BTC, ETH, SOL, BNB)
decides — a data-independent rule, reported with the rejection counts. Positions are not resized after entry.

## 6. Metrics

Grouped by purpose; backtest metrics and statistics are implemented in `src/trader/quant/stats.py` unless noted.

- **Return**: net total return; CAGR only when the sample exceeds one year; expectancy per trade, average win/loss,
  payoff ratio, profit factor; trade count, average holding period, exposure time, turnover.
- **Risk**: max drawdown, duration and recovery time; annualized volatility, downside deviation; Sharpe, Sortino,
  Calmar; worst day; longest losing streak; daily 95% CVaR / expected shortfall. Tail estimates remain noisy on short
  samples and are descriptive, not standalone gates.
- **Reliability**: PSR, DSR (with trial count), PBO when applicable, block-bootstrap confidence intervals (including
  paired Sharpe difference for the volatility overlay), in-sample → out-of-sample degradation, number of trials.
- **Predictive**: log loss and Brier score against the base rate, expected calibration error and reliability table,
  PR-AUC; ROC-AUC reported but never used as a proxy for profit; net return by predicted-probability bucket.
- **Execution** (paper/demo only): expected vs realized slippage, fees paid, rejects. Fill rate and latency become
  meaningful once limit orders are used.
- **Transaction attribution**: per-bar traded notional and modeled cost by signal entry, signal exit, in-position
  exposure resize, stop touch and gap stop. Fee, half-spread and slippage are allocated to each event by notional;
  cumulative cost is reported in bps of initial simulated equity. This is a simulator attribution, not measured fills.

### Thresholds

No threshold below is validated; all are **provisional** and live in `configs/app.yaml` (`validation:`), not in code.

| Gate | Provisional rule | Rationale |
|---|---|---|
| Enough trades (`min_evaluation_trades`, formerly `min_oos_trades`) | ≥ 100 closed trades in the evaluated period; each record names that period in `trade_count_gate.period` (whole development period for `trader backtest`, walk-forward test folds for the overlays, the development OOS window for portfolio runs) | Below that, win-rate and expectancy intervals are too wide to rank anything |
| Better than nothing | Lower bound of the 90% bootstrap CI of OOS Sharpe > 0 | "No trade" earns 0 without risk |
| Selection-robust | DSR ≥ 0.5 using the full trial count | Weak gate, intentionally; DSR ≥ 0.95 is the conventional strong claim |
| Cost-robust | Still positive at 2× costs | Our cost inputs are guesses |
| Model usable | Log loss and Brier better than base rate on every OOS fold, ECE ≤ 0.05 | Otherwise the probabilities carry no information |

Passing all gates would justify **paper trading**, nothing more.

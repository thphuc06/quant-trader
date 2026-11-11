# Validation framework v2 (low-frequency strategies)

**Status: APPROVED by the operator, 2026-11-11.**
Background and arithmetic: [reviews/2026-11-11-low-frequency-evaluation.md](reviews/2026-11-11-low-frequency-evaluation.md).
Applies to protocols registered after approval. Earlier experiment records keep the provisional gates they were
judged under (`gates_provisional` in each record).

## Why v1 is replaced

With four highly correlated coins and trend rules that trade a few times a year, information comes from the length of
the record and the true Sharpe, not from the number of trades. On 2 years of daily returns the v1 "90% CI lower bound
of Sharpe > 0" gate requires an observed annual Sharpe ≈ 1.25; a strategy with true Sharpe 0.8 passes ≈ 28% of the
time. The v1 trade-count gate (≥ 100) measures span badly, and nothing in v1 separates timing skill from simply being
long in a rising market.

## Gates

All statistics are computed on **UTC-daily net returns** of the portfolio backtest (`quant/portfolio.py`) over the
protocol's evaluation window, at the cost level the protocol names as primary.

| ID | Gate | Rule |
|---|---|---|
| G-timing | Timing skill | p ≤ 0.10 in the circular-shift test below |
| G-cost | Cost robustness | net total return > 0 at 2× the base cost assumption |
| G-ci | Sharpe interval | lowest 90% CI lower bound of annualized Sharpe across 5/10/20-day circular block bootstraps > 0. Each report states the implied observed-Sharpe threshold for its window length (≈ 1.25 for 2 years, ≈ 0.9 for 4 years) |
| G-dsr | Selection | DSR ≥ 0.5 with the binding N below; DSR with the raw registry count is always reported beside it |
| G-span | Enough closed trades | ≥ 30 closed trades in the window, all symbols combined |

### G-timing: definition

- **Inputs.** For each symbol s and day t: exposure e(s,t) = position value / equity at the close of day t−1 (what was
  held during day t); asset return a(s,t) = close-to-close return of day t; the strategy's daily modeled cost c(t) as a
  fraction of equity.
- **Reconstructed strategy return** g(t) = Σ_s e(s,t)·a(s,t) − c(t). This ignores intraday stop fills; the actual
  backtest return is reported beside it, and the test uses g for both the observed and the null statistic so that
  like is compared with like.
- **Benchmark** b(t) = Σ_s ē_s·a(s,t), where ē_s is the window-average of e(s,t): buy-and-hold at the strategy's
  average exposure. It pays one round trip per symbol at the same per-side cost, charged on the first and last day.
- **Statistic** D = mean_t(g) − mean_t(b), reported annualized (×365).
- **Null.** K = 2,000 circular shifts with seed 7. Each draws an offset u uniformly from [30, T − 30] days and shifts
  every symbol's exposure series and the cost series by the same u (cross-symbol structure and turnover preserved);
  the benchmark is unchanged because mean exposure is unchanged. D_k is computed the same way.
- **p-value** = (1 + #{D_k ≥ D}) / (1 + K). Pass: p ≤ 0.10.
- **Reading.** It asks whether the timing of the positions earned more than the same average exposure held
  passively, not whether the strategy made money.

### G-dsr: which N binds

- **Untouched sample, protocol committed before the data were downloaded or viewed:** N = number of configurations
  the protocol evaluates on that sample. The 65 earlier trials searched other data; their selection bias does not
  transfer to a sample none of them saw.
- **Any previously viewed sample (e.g. development OOS 2023-11 → 2025-11):** N = the full registry count.
- Daily return series of every new trial are stored in its record from approval onward, so an effective-N estimate
  (return clustering) can be added later; it is not used as a gate until the procedure is fixed in a new version.

### Concentration (reported, not a gate)

Net result after removing the two most profitable trades, and the share of total profit from the top 20% of trades.
Trend following normally earns from a few large trades with a 30–40% win rate, so this is information, not a pass/fail.

## Tiers

| Tier | Condition | Consequence |
|---|---|---|
| **REJECTED** | G-timing fails, or G-cost fails | Rule is dropped. No further trials of it, not added to the shadow log |
| **INCONCLUSIVE** | G-timing and G-cost pass, point Sharpe > 0, but G-ci, G-dsr or G-span fails (fewer than 30 trades is always at most INCONCLUSIVE, never REJECTED for that reason) | Frozen rule added to the shadow log with a registered start date; exit rules below |
| **VALIDATED** | All five gates pass | Eligible for the holdout check (below), then a paper-trading proposal to the operator. Nothing is automatic |

### Leaving INCONCLUSIVE

- **Observation:** 12 months of forward shadow log from the registered start date; the rule and parameters are
  frozen at registration and recorded in the shadow log's code commit.
- **Dropped early (→ REJECTED) if any of:**
  1. Implementation mismatch: a logged target differs from the frozen rule recomputed on the same candles and the
     difference is not explained by a documented bug fix. Investigate first; an unexplained mismatch ends the rule.
  2. After at least 6 months: cumulative shadow net return (logged signals, policy sizing, 2× costs) is below the 5th
     percentile of the backtest's block-bootstrap distribution for the same horizon.
  3. Measured execution costs (Demo/paper fills) exceed 2× the cost assumption.
- **After 12 months without a drop:** still INCONCLUSIVE. A year of shadow data detects only Sharpe ≳ 2.3, so it never
  promotes a rule by itself. The only path to VALIDATED is a new pre-registered protocol that pools untouched data
  (e.g. the back-extension plus the forward period) and passes all five gates on the pooled sample.

## Holdout (2025-11-10 → present)

- Opened **once**, by the operator (`--unlock-holdout`), only for a candidate already VALIDATED.
- One year cannot confirm a Sharpe below ≈ 2, so it is a **consistency check**. Pass requires all of:
  1. annualized net return at base costs not below the 5th percentile of the candidate's backtest block-bootstrap
     distribution for a one-year horizon;
  2. maximum drawdown ≤ 1.5× the backtest maximum drawdown and the policy drawdown floor not reached;
  3. turnover within ±50% of the backtest's annual turnover;
  4. no implementation mismatch.
- Fail → REJECTED. Pass → the operator may approve a paper-trading test; that is a decision, not an automatic step.

## Clarifications after the first use (H1, 2026-11-11)

These document what the code already does or what H1 revealed. None changed H1's outcome; any change of a rule
applies only to protocols registered afterwards.

1. **Two REJECTED cases not spelled out above:** a daily net Sharpe ≤ 0, or a timing p-value that cannot be computed
   (too few periods for the shift margin), are REJECTED.
2. **Reconstruction lag:** G-timing applies the exposure at the close of day t−1 to the close-to-close return of day t,
   while real entries and exits happen at the next open. The lag is the same in the observed and null statistics.
3. **Power of G-timing:** every report states the null's 95th percentile next to the strategy's reconstructed annual
   return. In H1 the 95th percentile (+9.7% to +13.5%/yr) exceeded the strategies' whole return at 4–7% exposure, so a
   REJECTED there means "timing skill not demonstrated", not "no edge". Research logs and the knowledge base must use
   that wording.
4. **G-dsr with a small binding N** (e.g. 2) filters almost nothing; it is reported, not relied on as a barrier.
5. **Drawdown lock as a confound:** a protocol must state how the policy's drawdown floor is simulated. With the
   current policy (entries blocked once equity is 10% below a peak that includes open profit, never released in a
   backtest) a long backtest is cut at the first such give-back. Pending operator decision.

## Implementation (after approval, builder session)

`configs/app.yaml` validation block gains the v2 parameters (trade floor 30, timing p 0.10, K 2,000, shift margin
30 days); a `quant/stats` timing test with unit tests on synthetic data (a known-timing series must pass, a shuffled
one must not); records store daily return and exposure series. The v1 gates remain readable for old records.

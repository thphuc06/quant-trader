---
name: strategy-research
description: Form and evaluate one falsifiable strategy hypothesis as a registered trial, without touching the locked holdout. Use for "test this strategy", "backtest X", "is this idea any good".
---

# One research trial

Every backtest is logged as a trial and lowers the deflated Sharpe of *every* result. Cheap diagnostics first,
trials second, and one trial per approved hypothesis.

1. **Hypothesis.** Write the economic reason it should work, the universe, timeframe, holding period and a rejection
   criterion fixed *before* running. Cite sources; label untested ideas as hypotheses.
2. **Prior work.** `get_experiment_results` — has this (or a close variant) been run? Read `research/research-log.md`
   and `research/ml-and-validation.md` (gates, cost model, known limitations). A variant tuned on already-viewed
   out-of-sample data is not new evidence.
3. **Cheap diagnostics before a trial.** If the question is "why does X lose", look at existing records
   (turnover attribution, costs, trade counts) before proposing a new configuration.
4. **Ask the operator** before `run_backtest` (it asks for permission anyway): state the hypothesis, the parameters
   fixed in advance and why this is one trial, not a sweep.
5. **Report** net of costs: trades, Sharpe with its confidence interval, max drawdown, result at 2x costs, DSR with
   the trial count, comparison with `buy_and_hold` and `no_trade`, and the limitations block. Negative results are
   results.
6. **Never** unlock or inspect the holdout (`--unlock-holdout` is human-only). A candidate is "validated" only if
   every configured gate passes, and that only justifies paper trading.

# Protocol: volatility target with a no-trade band

Status: preregistered before running this variant on the repository's candles (2026-11-10).

## Motivation and literature

The first volatility-scaling experiment recalculated target exposure every hourly bar and produced roughly 56–180× annual traded notional. The question for this trial is whether a fixed rebalancing boundary can reduce that activity while retaining the risk-management behavior. Bai et al. (online 2025; 2026 volume) study rebalancing boundaries for target-volatility strategies under transaction costs. Holden & Holden show theoretically that proportional transaction costs can imply a no-trade region. Neither result establishes a profitable rule for individual crypto assets on hourly bars; both motivate a bounded execution-policy experiment, not an alpha claim. See [source register](../source-register.md).

## Frozen specification

- Candidate: the existing long/flat signal multiplied by the existing trailing 30-day realized-volatility scale; exposure remains in `[0, 1]`.
- Execution change: at each next-bar open, compare desired exposure with actual marked exposure. If the baseline signal is long and the absolute difference is at most **0.05 of equity**, leave units unchanged. If it exceeds 0.05, rebalance to the desired target. A flat baseline signal always exits fully. This is a symmetric 5 percentage-point no-trade band; it is not calibrated to these OOS results.
- Universe: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT; both existing baseline signals. One fixed policy family, eight symbol/baseline evaluations.
- Validation: same rolling 730-day train / 90-day test / 90-day step protocol, 30-day realized volatility, 13 bps per-side base costs, 2× cost stress, next-open execution, and bootstrap setup as the initial overlay evaluation.
- Holdout: dates from `2025-11-10` remain excluded and locked.
- Primary research check: candidate annual turnover is at least 25% lower than the unbanded volatility overlay, measured per pairing; report the share of pairings meeting it.
- Return guardrail for continued study: net cumulative return is no more than 5 percentage points below the unbanded overlay in each pairing. This is a descriptive preregistered guardrail, not a statistical equivalence test.
- Existing six provisional promotion gates remain mandatory for any paper-test consideration. This development OOS was already inspected for the first overlay, so even a favorable result is exploratory and cannot validate the band or justify paper trading.
- No tuning of the 5% boundary, no extra variants, no opening holdout, and no claim of alpha based on this run. Any later confirmation requires untouched forward data or a newly declared sample.

## References

- Bai, Z., Pachamanova, D., Steblovskaya, V., et al. (2025 online; 2026 volume), “Target volatility strategies: optimal rebalancing boundary for transaction cost minimization,” *Financial Markets and Portfolio Management*. https://doi.org/10.1007/s11408-025-00486-5
- Holden, H. & Holden, L. (2013), “Optimal rebalancing of portfolios with transaction costs,” *Stochastics*. https://doi.org/10.1080/17442508.2011.651219

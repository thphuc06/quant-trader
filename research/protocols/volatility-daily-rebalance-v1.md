# Protocol: daily volatility resizing

Status: preregistered before running this variant (2026-11-10).

## Motivation

The hourly volatility overlay produced 55–180× annual traded notional. A 5 percentage-point no-trade band reduced turnover by only 0.8–2.8%, so the next mechanism under test is the rebalance clock itself. Literature on target-volatility rebalancing boundaries and no-trade regions supports testing lower adjustment frequency under costs, but does not prescribe an optimal crypto cadence or validate this rule; see [source register](../source-register.md).

## Frozen specification

- Candidate signal: existing baseline long/flat state multiplied by the existing 30-day realized-volatility scale; exposure remains in `[0, 1]`.
- Sizing cadence: update the volatility multiplier once per UTC day, using the just-closed hourly candle at the day boundary; execute that adjustment at the next hourly open. Hold units between daily sizing updates; do not rebalance for mark-to-market drift during the day.
- Signal behavior: baseline entries and exits remain event-driven at the next hourly open. A flat baseline signal closes the position immediately, independent of the daily schedule. A new long entry uses the latest daily volatility multiplier.
- Universe: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT; both existing baseline signals. One fixed policy family, eight symbol/baseline evaluations.
- Validation: same rolling 730-day train / 90-day test / 90-day step folds, 30-day realized volatility, 13 bps per-side base costs, 2× cost stress, next-open execution and bootstrap setup as prior overlay runs.
- Holdout: `2025-11-10` onward remains locked and excluded.
- Primary check: at least 25% lower annual turnover than the hourly volatility overlay in each pairing; report per-pair results and how many meet it.
- Return guardrail: net cumulative return no more than 5 percentage points below the hourly overlay per pairing.
- All existing six promotion gates remain required for paper-test consideration. The development OOS was previously inspected, so even favorable results are exploratory and cannot establish confirmation or alpha.
- No cadence sweep, no parameter tuning on these dates, no holdout unlock, no live or demo order placement. Forward shadow observations are required before considering any candidate further.

## References

- Bai, Z., Pachamanova, D., Steblovskaya, V., et al. (2025 online; 2026 volume), “Target volatility strategies: optimal rebalancing boundary for transaction cost minimization,” *Financial Markets and Portfolio Management*. https://doi.org/10.1007/s11408-025-00486-5
- Holden, H. & Holden, L. (2013), “Optimal rebalancing of portfolios with transaction costs,” *Stochastics*. https://doi.org/10.1080/17442508.2011.651219

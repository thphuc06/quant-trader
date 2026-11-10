# Strategy landscape, instruments, features

Labels (`verified`, `literature`, `background`, `hypothesis`) are defined in [README.md](README.md).
Source IDs refer to [source-register.md](source-register.md).

## 1. Instrument: Spot first

| Option | For | Against | Decision |
|---|---|---|---|
| **Binance Spot** | No leverage, no liquidation, no funding; simplest order/risk model; official Demo Mode with live-like filters (BN-DEMO, verified) | Long/flat only (no shorting without margin) | **MVP** |
| USDⓈ-M Futures | Shorting, funding/OI as native signals | Leverage + liquidation + funding accounting multiply the ways to lose money and the surface to test | Deferred. We read funding/OI as *context* only (BN-FUT, verified) |
| COIN-M Futures | — | Inverse payoff, thinner books; no reason for a first system | Rejected |

Consequence: `SHORT_CANDIDATE` exists in the decision schema as an analytical outcome but **cannot be executed**;
the risk engine only allows `BUY` to open and `SELL` to reduce an existing position.

## 2. Horizon: 1h decisions with 4h/1d context

Cost arithmetic (hypothesis built on FEE-SQ, low–medium confidence): at ~10 bps taker per side a round trip costs
~20 bps in fees before spread and slippage. A 5m BTC bar typically moves a comparable amount, so a 5m/15m strategy
must predict most of each bar's move just to break even, and it also needs infrastructure (continuous collector,
low-latency execution) that an interactive, human-approved Claude Code session cannot provide. A human approval step
alone adds minutes of latency.

→ Signals are computed on **closed 1h bars**; 4h and 1d are used for regime/context. 5m/15m and order-book
microstructure are **out of MVP scope**. This is the single biggest change from the "use all timeframes" framing.

## 3. Universe

`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT` on Spot.

- Deep books and long histories, so cost assumptions are least wrong and samples are longest.
- All four existed for the whole intended backtest window, which avoids listing/delisting bias *inside this universe*.
  It does **not** remove selection bias: these are today's survivors, chosen with hindsight. Results must be read as
  "conditional on having picked large caps that survived".
- Highly correlated: four positions are closer to one bet than to four. The risk policy therefore caps total exposure
  and open positions rather than pretending to diversify.

Scanning hundreds of coins is rejected for the MVP: it multiplies trials (overfitting) and brings illiquid names
where our cost model is wrong.

## 4. Strategy families

| Family | Economic hypothesis | Fits when | Data | Main failure mode | Verdict |
|---|---|---|---|---|---|
| **Time-series momentum / trend** | Slow diffusion of information, attention-driven flows (LT, literature, medium) | Trending regimes | OHLCV | Whipsaw in ranges; crowded; evidence predates 2019 and is at daily/weekly frequency | **Baseline 1** |
| **Breakout / volatility expansion** | Volatility clusters; range breaks coincide with new information | Compression → expansion | OHLCV | False breakouts, slippage at the worst moment | **Baseline 2** |
| Mean reversion | Liquidity provision is compensated | Range, high liquidity | OHLCV (+book) | Catching knives in trends; long-only spot cannot fade rallies | Deferred; only as a regime-conditional study |
| Market-structure / price-action rules | — | — | OHLCV | Mostly discretionary; many degrees of freedom → overfitting | Used as *features* (range location), not a strategy |
| Order-book / trade-flow | Short-lived imbalance | Seconds–minutes | L2 + trades stream | Needs a 24/7 collector, large storage; horizon far below ours | **Rejected for MVP** (taker-buy ratio from klines kept as a cheap flow proxy) |
| Derivatives context (funding, OI, basis) | Positioning extremes precede squeezes | Crowded positioning | Futures REST | Regime-dependent, short public history via REST | Context only, not a signal |
| News / event-driven | Information arrival | Scheduled or surprising events | Search + official feeds | Latency vs. bots; no point-in-time archive → cannot be backtested honestly | **Risk filter and context only**; never an entry trigger in MVP |
| ML-assisted hybrid | Non-linear combination of weak features | If features carry signal | Features above | Overfitting, leakage, non-stationarity | Baseline pipeline built; **not used for decisions** until it beats the rule baselines out of sample |

No indicator or strategy here is claimed to forecast price. Each baseline is a hypothesis with a rejection test
(see [ml-and-validation.md](ml-and-validation.md) §4).

### Baseline definitions (implemented in `src/trader/strategies/`)

- `tsmom_trend_v1` — long when EMA(fast) > EMA(slow) **and** the N-bar return is positive; flat otherwise; ATR stop.
- `donchian_breakout_v1` — long when the close exceeds the prior N-bar high; exit when it falls below the prior M-bar low; ATR stop.
- Benchmarks: `buy_and_hold`, `no_trade` (flat; return 0, the bar every strategy must clear after costs).

Rejection criteria (provisional): a baseline is dropped if, on walk-forward out-of-sample data net of costs, its
Sharpe confidence interval includes zero **and** it does not improve max drawdown versus buy-and-hold, or if DSR
(with the true number of trials) is below 0.5. These thresholds are judgement calls and are marked provisional.

## 5. Features: role and redundancy

We keep one or two features per *information type* instead of every famous indicator. Implemented catalogue:
`src/trader/features/registry.py`.

| Role | Kept | Dropped (and why) |
|---|---|---|
| Trend direction | `ema_spread` (EMA fast/slow − 1), `tsmom_n` (N-bar log return) | SMA crosses, MACD, Supertrend — all are smoothed-return transforms, highly collinear with the two kept |
| Trend strength | `adx_14` | — |
| Momentum/oscillation | `rsi_14` | Stochastic, ROC — near-duplicates of RSI / `tsmom_n` |
| Volatility level | `atr_pct`, `rv_24` (realized vol) | Bollinger bandwidth ≈ rolling std ≈ `rv`; Parkinson/Garman-Klass deferred (worth it only if vol forecasting becomes a target) |
| Volatility regime | `rv_ratio` (short/long), causal rolling percentile of `rv` | — |
| Range location / structure | `donchian_pos_n` | Hand-drawn S/R, "break of structure" — not objectively computable |
| Participation | `rel_volume` | OBV — cumulative, non-stationary |
| Flow proxy | `taker_buy_ratio` (from kline fields, BN-MD verified) | Full CVD / book imbalance — needs trade/L2 streams |
| Anchored value | `vwap_dev_24` | — |
| Positioning context | funding rate, open interest (read on demand) | Liquidations — no reliable free point-in-time source identified |

Not included: BTC dominance, DXY/rates, on-chain, social sentiment. No licensed point-in-time source was selected;
they are listed as deferred rather than faked.

Leakage controls: every feature is computed with trailing windows only (`rolling`, `ewm(adjust=False)`), on closed
bars; a test recomputes features on truncated history and requires identical values (`tests/unit/test_features.py`).

## 6. When the right answer is NO_TRADE

- Data stale, missing, or internally inconsistent; exchange/risk state unknown (fail closed).
- Spread or liquidity outside policy.
- Regime `range` with no validated strategy for ranges.
- Conflicting evidence that cannot be resolved; a scheduled high-impact event inside the holding horizon.
- Expected move not clearly larger than round-trip cost.
- No strategy with out-of-sample support is signalling. Because nothing is validated yet, **`NO_TRADE`/`WATCH` is the
  default outcome of this system today**.

## 7. Recommendation matrix

| Candidate | Testable with our data? | Cost-robust at 1h? | Complexity | Decision |
|---|---|---|---|---|
| Spot, 1h decisions, 4 large caps | yes | plausible | low | **Adopt** |
| `tsmom_trend_v1` | yes | plausible | low | **Baseline, test** |
| `donchian_breakout_v1` | yes | plausible | low | **Baseline, test** |
| Regime filter (rule-based) | yes | n/a | low | **Adopt as context**; ablation decides if it gates entries |
| Logistic / gradient-boosting on triple-barrier labels | yes | unknown | medium | **Build pipeline, do not trade on it** |
| Mean reversion | partly | unknown | medium | Defer |
| Futures (short side, funding carry) | yes | unknown | high | Defer |
| 5m/15m, order-book | no (no collector history) | unlikely | high | Reject for MVP |
| News as entry trigger | no (no PIT archive) | unknown | high | Reject for MVP; keep as filter |
| HMM / sequence models | marginal | unknown | high | Reject until simple baselines show signal |

## 8. Answers to the "challenge the idea" questions

- **Claude reads raw data or precomputed snapshots?** Precomputed. Indicators, sizing and risk numbers come from tested
  code; Claude reasons over compact, timestamped summaries. Raw candles are capped at 200 rows per call.
- **Many agents from day one?** No. One analyst plus two read-only reviewers (news, risk). More agents doing the same
  job would manufacture agreement, not evidence.
- **One model or per-regime models?** One simple model per horizon first; per-regime models split an already small
  sample.
- **Does news create alpha?** Unknown and untestable without a point-in-time archive. It is used to *veto*, and every
  item is stored with `observed_at` so an event study becomes possible later.
- **Which indicators overlap?** See §5.
- **Order book in MVP?** No (§4).
- **Spot or futures? 1h/4h or 5m/15m?** Spot; 1h/4h (§1–2).
- **Most verifiable direction for one person?** Low-frequency rules on liquid majors: years of free data, few parameters.
- **Freqtrade / VectorBT / Backtrader / custom?** Custom small bar simulator — see [ml-and-validation.md](ml-and-validation.md) §5.
- **What lives in Git vs runtime?** Code, configs, reviewed knowledge, compact experiment summaries in Git; candles,
  SQLite DB, models, logs under `data/` (ignored).
- **May Claude invent rules from the trade log?** Only as *candidate lessons*. Promotion needs a linked experiment and
  a human at a terminal; see ADR 0004.

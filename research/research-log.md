# Research log

All entries 2026-11-10 (UTC+7 working day). Tools: Claude Code built-in WebSearch/WebFetch, direct HTTP calls for API probes.

| # | Action | Outcome |
|---|---|---|
| 1 | Inspected the directory | Only the master prompt; no git repo; Python 3.13, git, Docker present; `uv` absent → plain `venv` + `pip` |
| 2 | `pip index versions mcp` | Latest is **2.3.0**, a major version beyond what the prompt's examples assume |
| 3 | Read Binance general REST info | Found market-data-only host `data-api.binance.vision`; 429/418 semantics; 5XX = unknown execution state |
| 4 | Looked for Spot Testnet page | The page had no URLs; search led to **Demo Mode**, a separate environment with live-like prices → chosen over Testnet |
| 5 | Read Claude Code hooks, skills, permissions, MCP, subagents, headless docs | Confirmed formats; learned a repo cannot self-approve its MCP server and that `-p` skips trust prompts |
| 6 | Read MCP SDK front page, migration guide, testing page | `FastMCP` → `MCPServer`; in-memory `Client` for tests |
| 7 | Read Binance market-data, trading, account, filters, request-security pages | Endpoint table in `binance-api-research.md` |
| 8 | Searched DSR, PBO, crypto momentum papers | Citations confirmed; full texts not re-read (marked `literature`/`background`) |
| 9 | Compared search APIs; read Tavily and Brave references | Pricing claims conflict across secondary sources → no dependence on free tiers |
| 10 | Searched Binance fees | Official fee page not retrieved; 10 bps taken as a provisional assumption |
| 11 | Probed `data-api.binance.vision` klines and `fapi.binance.com` premiumIndex/openInterest | All HTTP 200 from this machine |
| 12 | Read WebSocket streams page | Base URLs and kline payload confirmed; ping/lifetime section truncated |

## What changed relative to the master prompt

| Prompt suggestion | Decision | Why |
|---|---|---|
| PostgreSQL + Parquet/DuckDB, docker-compose | **SQLite (WAL) + Parquet**, no containers | One user, one machine, one writer. Postgres adds an always-on service and a failure mode ("database unavailable") without a matching need. The repository layer is small enough to port later |
| `apps/`, `core/`, `exchanges/`, `mcp_server/` top-level trees | One package `src/trader/` | A single installable package keeps imports and tests simple |
| ~45 MCP tools | ~25 task-shaped tools | Fewer, larger tools are easier for the model to choose correctly and keep context small |
| 9 skills, 4 subagents | 4 skills, 2 subagents | Each removed item duplicated another's job |
| uv + `uv.lock` | `uv` + pinned `requirements.lock` | `uv.lock` is not maintained; the committed requirements lock remains the dependency source |
| News subsystem with dedup/entity resolution/event classification | Provider abstraction + point-in-time archive + safe fetch | No archive exists to validate anything richer |
| ML "prediction service" | Offline pipeline + model card; predictions exposed but flagged unvalidated | Nothing should look more trustworthy than it is |
| Freqtrade/VectorBT/Backtrader | Custom simulator | See `ml-and-validation.md` §5 |
| One-time approval token passed through the model | Approval recorded by a **human-only CLI** (needs a TTY); the server consumes it | A token that travels through the LLM context can be replayed or leaked by the LLM |
| Futures adapter, user-data stream, macro calendar, on-chain | Deferred | Listed under deferred scope in `architecture/system-design.md` |

## Open questions carried forward

- Real fee tier of the operator's account (read from `GET /api/v3/account` once keys exist).
- Measured slippage in Demo Mode vs the 2 bps assumption.
- Whether Demo Mode klines are reliable enough to drive signals (we use production market data instead).
- An official, stable feed for exchange announcements and a licensed macro calendar.

## Follow-up checkpoint — harness and volatility overlay (2026-11-10)

- Completed `.claude/settings.json`, `.mcp.json`, `CLAUDE.md`, four skills and two read-only subagents. Only the existing `SessionStart`, `PreToolUse` and `PostToolUse` hooks are enabled. Write/transaction tools still require a permission prompt; MCP config contains no credentials.
- Created `.venv` with Python 3.13 using `uv`, installed `requirements.lock`, and used that environment for the integration suite, `ruff`, `mypy`, `trader doctor`, ingestion and backtests. The full test suite passed; `trader doctor` passed with 27 MCP tools registered and live trading locked.
- Re-ingested Binance Spot 1h klines for BTCUSDT, ETHUSDT, SOLUSDT and BNBUSDT: 43,798 bars per symbol over the five-year window. Each stream has one missing bar; quality is marked degraded and no bar was fabricated or repaired.
- Evaluated the volatility-scaled exposure challenger against `tsmom_trend_v1` and `donchian_breakout_v1`, with 730-day train / 90-day test rolling folds (9 folds), 30-day realized volatility, next-bar-open execution, exposure in [0, 1], notional-based costs of 13 bps per side and 2× cost stress. OOS development window: 2023-11-11 15:00 UTC through 2025-11-09 23:00 UTC. The 2025-11-10 holdout remained locked.
- Sharpe point estimates improved in all eight symbol/baseline pairs. However, no pair passed all six provisional gates: 2/8 stayed positive at 2× costs, 4/8 had fewer than 100 trades, and turnover ranged from 56× to 180× notional per year. All 90% paired Sharpe-difference intervals were above zero in this exploratory batch, but DSR after counting 49 registered trials ranged from 0.19 to 0.66 and OOS data have now been viewed. Treat the intervals as exploratory and do not claim alpha or use these results to tune a new strategy on the same period.
- Decision: no paper-test candidate; keep `NO_TRADE` / `WATCH`. Detailed fold-level metrics and run assumptions are in the eight successful `volatility_overlay` records under `research/experiments/`. Earlier failed invocations remain in the experiment registry as failures.
- The supplied literature lead is recorded as KZ26 in `source-register.md`: its index-level daily result is regime-dependent and does not establish efficacy for this coin-level 1h overlay.

## Follow-up checkpoint — fixed no-trade band (2026-11-10)

- Reviewed Bai et al. on target-volatility rebalancing boundaries and Holden & Holden on no-trade regions under proportional transaction costs. These motivate limiting small exposure adjustments but do not set a crypto-specific hourly threshold; sources and limitations are in `source-register.md`.
- Before running the variant, registered `research/protocols/volatility-rebalance-band-v1.md`: a fixed symmetric 5 percentage-point equity band, force-flat exit, same eight pairings and walk-forward/cost protocol, at least 25% turnover reduction as the primary check, and a 5pp net-return-loss guardrail. No threshold sweep was run.
- Implemented the band against actual marked exposure at each next-bar open. Within the band, units remain unchanged; a flat base signal exits immediately. The CLI command is `trader research-volatility-band`; experiment records identify the protocol and mark this previously viewed OOS as exploratory.
- The same development OOS (`2023-11-11 15:00 UTC`–`2025-11-09 23:00 UTC`) was re-evaluated across four symbols and two baselines. Turnover fell only 0.8%–2.8% per pairing, below the preregistered 25% target. All 8/8 net-return differences versus the unbanded overlay were within the −5pp guardrail, but annual turnover remains roughly 55×–178× notional; no pairing passed all six current gates. DSR ranged 0.19–0.65 after counting the full 57 trials.
- Decision: the fixed 5% band did not solve the turnover issue. Do not tune more band widths on these OOS dates and do not paper-test this candidate; keep `NO_TRADE` / `WATCH`. Holdout from `2025-11-10` stayed locked.

## Follow-up checkpoint — daily-only volatility resizing (2026-11-10)

- Registered `research/protocols/volatility-daily-rebalance-v1.md` before running: update the realized-volatility sizing multiplier once at the UTC day boundary, hold units between those updates, but preserve baseline hourly entries/exits. Fixed checks: at least 25% less turnover than hourly sizing and net return no worse than −5pp per pairing. No cadence sweep was run.
- Implemented a causal UTC-day signal wrapper and an execution schedule in the bar simulator. Added `trader research-volatility-daily`; it creates eight family-counted records, includes paired bootstrap comparisons against hourly sizing, and does not touch the locked holdout.
- Exploratory rerun over the same development OOS found only 0.5%–1.5% lower turnover per pairing; none met the 25% target. All 8/8 net-return differences were within −5pp, but turnover remained 55×–179× per year and no pairing passed all six existing gates. DSR after the full 65-trial count was 0.19–0.64.
- Decision: daily volatility resizing also fails to materially reduce turnover. Do not spend a 90-day shadow period on this candidate and do not tune cadence on the viewed OOS. Keep `NO_TRADE` / `WATCH`; a new candidate needs a materially distinct, preregistered hypothesis and later untouched forward confirmation.

## Diagnostic checkpoint — turnover attribution (2026-11-10)

- Added per-bar attribution for baseline entry, baseline exit, overlay resize, stop touch and stop gap. Turnover uses the existing notional / pre-trade equity denominator. Fee, half-spread and slippage costs are allocated from the existing cost config by executed notional and stored as cumulative bps of initial simulated equity.
- Replayed the original hourly baseline and volatility overlay over the same development walk-forward folds. Records use kind `turnover_attribution`, have `diagnostic_only: true` / `counts_toward_dsr: false`, and are ignored by trial accounting.
- Across the eight overlay pairings, volume-weighted turnover shares were: baseline entry 49.4%, baseline exit 37.2%, in-position overlay resize 1.5%, stop touch 11.9%, stop gap 0%. Mean total overlay turnover was 112.7×/year. Modeled costs averaged 3,613 bps of initial equity per pairing over the evaluation period under the provisional 13 bps/side assumption (2,779 fee, 278 half-spread, 556 slippage bps).
- Replay reproduced each original hourly overlay's net return and total turnover exactly; all holdouts remained locked. This is descriptive attribution on already-viewed OOS data, not a new strategy trial or alpha evidence.

## Builder checkpoint — shadow log, research reliability (2026-11-11)

- Forward shadow log (`trader shadow-log`, MCP `get_shadow_log`): append-only rows per closed 1h bar with features,
  regime, baseline targets, validation status, data quality, commit and `logged_at`. First live run logged the
  2026-11-10 16:00 UTC bar for all four symbols. The hourly schedule is for the operator to register
  (docs §9); until then the log only grows when run by hand.
- Portfolio backtester (`quant/portfolio.py`) mirrors the risk engine's entry checks on shared capital; sizing parity
  with `risk.compute_sizing` is tested. Daily-return block bootstrap reports 5/10/20-day blocks side by side.
- Gate `min_oos_trades` → `min_evaluation_trades`, with the evaluated period named in each new record. DSR trial
  accounting now excludes `buy_and_hold`/`no_trade` and counts ML evaluations: the registry held 63 counted trials
  before the run below (65 under the old rule).

## Preregistered trial — daily-bar baselines through the portfolio (2026-11-11)

Protocol [daily-baselines-v1](protocols/daily-baselines-v1.md) committed in `c95654c` before the runner existed;
runner committed in `b9d0497`; run once with a clean tree. Window 2023-11-12 → 2025-11-10 (exclusive); holdout locked.
Daily bars are complete UTC days aggregated from stored 1h candles: they equal native 1d klines on all 136 overlapping
development days for BTC/ETH (SOL/BNB have no native 1d data); two warm-up days were dropped (partial first day,
2023-03-24 exchange halt). Risk policy v0.1.0, 13 bps/side, fixed BTC > ETH > SOL > BNB tie-break, stops clipped
at the policy's 10% maximum.

| | tsmom daily (`exp_fadf9d50`) | donchian daily (`exp_9aecbe3c`) | tsmom 1h ref (`exp_7168d0fd`) | donchian 1h ref (`exp_158cc592`) |
|---|---:|---:|---:|---:|
| Net return (2 y) | +12.7% | +10.0% | +7.0% | +2.3% |
| Net return at 2× costs | +11.8% | +9.4% | — | — |
| Max drawdown | −8.2% | −5.1% | −10.6% | −10.4% |
| Sharpe (annualized, per bar) | 0.80 | 0.82 | 0.41 | 0.16 |
| Lowest 90% CI lower bound (5/10/20-day blocks) | −0.37 | −0.38 | −1.04 | −1.06 |
| Closed trades | 37 | 21 | 196 | 190 |
| Turnover / year | 1.9× | 1.1× | 30.3× | 30.9× |
| Modeled cost, bps of initial equity / year | 28 | 15 | 420 | 420 |
| Mean gross exposure | 10.8% | 7.9% | 8.1% | 13.2% |
| DSR (65 trials, daily units) | 0.40 | 0.41 | not a trial | not a trial |

Gates: **H1 turnover cut passes** for both (6.4% and 3.6% of the hourly reference). **G4 (positive at 2× costs)
passes** for both. **G1 fails** (37 and 21 trades vs 100), **G2 fails** (every block length puts the Sharpe CI
lower bound below zero), **G3 fails** (DSR 0.40 / 0.41 < 0.5). No candidate passed all gates.

Interpretation, as fixed in the protocol: the slower clock fixes the cost mechanics (costs fall from ~420 to 15–28 bps
of equity per year), but the result is **inconclusive and not a paper candidate**: with 21–37 trades the Sharpe and
DSR estimates cannot rank anything, and the point estimates come from a period viewed by many earlier trials. No
threshold, universe or window change will be made on this period.

Notes the protocol did not anticipate (descriptive only):
- The 0.5% risk-per-trade sizing with wide daily stops keeps positions small (all entries bound by the risk budget;
  mean exposure 8–11%), so returns are a small fraction of buy-and-hold moves in either direction.
- Both hourly references hit the policy's 10% drawdown floor and were then blocked from new entries (tsmom: 20,375
  rejected entry-bars, invested 27% of the time; donchian: 4,878, invested 48%). Their turnover is therefore
  understated, so the measured turnover cut is conservative. It also means the hourly baselines would have tripped
  the account's drawdown control under the current policy.
- Slot limits mattered for the daily candidates (455–729 entry-bars rejected by `max_open_positions`), so the
  fixed tie-break shaped which symbols were traded; the per-symbol split is in the records.

Decision: keep `NO_TRADE` / `WATCH`. Possible next evidence is forward only, e.g. adding the two daily rules to the
shadow log. That changes the plan and is left to the operator.

**Correction (later 2026-11-11).** The table above came from code with a defect: a stop clipped to exactly the 10%
policy maximum was rejected by float rounding, so the protocol's clip rule skipped 5 (tsmom) and 11 (donchian) daily
entries (fixed in `4919c5f`; found while inspecting the H1 run below, after these results were seen). Corrected
re-runs supersede the four records; the old records are kept and excluded from the DSR count.

| Corrected | tsmom daily (`exp_4a288b04`) | donchian daily (`exp_f37a418c`) | tsmom 1h ref (`exp_0851cd06`) | donchian 1h ref (`exp_4af2060a`) |
|---|---:|---:|---:|---:|
| Net return (2 y) / at 2× costs | +6.3% / +5.6% | +10.2% / +9.7% | +7.0% | +2.1% |
| Max drawdown | −7.1% | −4.6% | −10.6% | −10.4% |
| Sharpe | 0.55 | 0.91 | 0.41 | 0.15 |
| Closed trades | 40 | 17 | 196 | 191 |
| Turnover / year (ratio to 1h) | 2.1× (6.8%) | 0.9× (2.9%) | 30.3× | 31.0× |
| DSR (67 trials) | 0.28 | 0.47 | — | — |

Same gate outcome as before (turnover cut and 2× cost pass; trade count, Sharpe CI and DSR fail): inconclusive under
the v1 gates. The tsmom point estimate roughly halved, which is a reminder of how fragile a 2-year, 20–40-trade
result is.

## Validation framework v2 and preregistered H1 — frozen daily rules on 2017–2021 (2026-11-11)

- A literature/methods review ([reviews/2026-11-11-low-frequency-evaluation.md](reviews/2026-11-11-low-frequency-evaluation.md))
  showed that the v1 gates cannot pass a realistic low-frequency strategy on 2 years of data (a true Sharpe 0.8
  passes the CI gate ≈ 28% of the time) and do not separate timing from being long in a bull market. The operator
  approved [validation-framework-v2.md](validation-framework-v2.md): timing-skill test against exposure-matched
  buy-and-hold, 2× cost, Sharpe CI with the implied threshold stated, DSR with N bound to the sample, ≥ 30 trades,
  and tiers REJECTED / INCONCLUSIVE / VALIDATED.
- [H1 protocol](protocols/daily-baselines-backext-v1.md) committed in `ebd0036` before any 2017–2021 candle was
  downloaded; framework code `77492cd`. Data: Binance BTCUSDT/ETHUSDT 1h from 2017-08-17 04:00 UTC, 36,997 bars
  each, 27 gaps / 127 missing hours (exchange outages) → 29 incomplete days dropped per symbol; daily aggregation
  equals native 1d klines on all 1,518 compared days. Window 2017-12-01 → 2021-11-11, 2× costs (26 bps/side) primary.
- First run (`exp_e8b28779`, `exp_7fc822fd`) had the clip defect (9 and 8 skipped entries) and is superseded.
  Corrected run, the result of record:

| H1 (2× costs) | tsmom daily (`exp_28ca944d`) | donchian daily (`exp_64ca1d36`) |
|---|---:|---:|
| Net return (≈ 3.95 y), 2× / 1× costs | +39.8% / +41.2% | +30.5% / +31.5% |
| Max drawdown | −19.5% | −15.2% |
| Sharpe | 0.74 | 0.73 |
| Closed trades | 19 | 14 |
| Mean exposure | 7.3% | 4.3% |
| Timing excess vs exposure-matched buy & hold, annualized | +1.8% | +4.2% |
| **Timing p-value (K = 2,000 shifts)** | **0.22** | **0.17** |
| Lowest 90% CI lower bound of Sharpe (implied threshold ≈ 0.9) | −0.08 | −0.17 |
| DSR binding N = 2 / raw N = 67 | 0.92 / 0.33 | 0.92 / 0.33 |
| Net P&L without the two best trades (fraction of equity) | +0.026 | −0.022 |

- Gates: G-cost and G-dsr pass; **G-timing fails** for both; G-ci and G-span fail. Tier: **REJECTED** for both.
- Decision, fixed in the protocol: both rules are dropped (no further trials on any data, not added to the shadow log),
  and the daily-trend direction ends for this desk. Keep `NO_TRADE` / `WATCH`. The rules are not re-run to "rescue"
  the result.
- **What the result shows: timing skill was not demonstrated. It does not show that daily trend has no edge.**
  - The test was weak by construction. The 95th percentile of the null was +13.5%/yr (tsmom) and +9.7%/yr (donchian)
    while the strategies' whole reconstructed return was ≈ 8.5–8.9%/yr, so passing required roughly twice the return
    the strategies made at 5–7% exposure.
  - The test measured "rules + the drawdown lock", not the rules alone. The policy's 10% floor was reached in mid-2021;
    the last day with a position was 2021-06-21 (tsmom) and 2021-05-19 (donchian), after which entries were blocked
    for the remaining 5–6 months (≈ 10–12% of the window). The size of this effect was not measured.
  - The 10% floor did not bound drawdown (−19.5%, −15.2%): it only blocks new entries, and the equity peak includes
    open profit, so giving back part of a large trend trade both breaches it and locks the account for good.
  - G-dsr with the binding N = 2 (0.92) filters almost nothing; raw-registry DSR was 0.33. Not decisive here, but it
    should not be read as a barrier in future protocols.
  - Profits came from very few trades (removing the two best leaves ≈ 0).
- Reproducibility note: the four corrected daily-baselines-v1 records (`exp_0851cd06`, `exp_4a288b04`,
  `exp_4af2060a`, `exp_f37a418c`) carry `working_tree_dirty: true`. The code at that moment was exactly `4919c5f`;
  the tree was dirty only because the two corrected H1 records written minutes earlier were not yet committed
  (commit `29c010e` adds only experiment JSON and docs). Re-running from `4919c5f` reproduces them.
- Code/document gaps found in review (clarified in framework v2, no effect on H1's outcome): the code also assigns
  REJECTED when the daily Sharpe is ≤ 0 or the timing p-value cannot be computed; the timing reconstruction applies the
  previous close's exposure to the full close-to-close day, one day later than the real open-price entries/exits.

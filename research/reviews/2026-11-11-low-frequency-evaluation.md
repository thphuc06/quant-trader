# Review: evaluating low-frequency crypto strategies (2026-11-11)

Provenance: literature and methods review requested by the operator and produced in a separate Claude session
(trader role, research budget 8 searches / 40 tool calls). Filed verbatim below, with corrections and a check of
the arithmetic by the builder session. Evidence labels are the reviewer's: *Background* = abstract, summary page or
search snippet retrieved in that session; *Recalled* = from model memory, not retrieved, numbers must be checked
before citing; *Claim* = secondary source or blog. **No paper was read in full.** Treat every empirical number in
§1, Q1, Q4 and Q5 as unverified until upgraded.

## Corrections and checks (builder session)

- The prompt given to the reviewer said "data up to 2025-11-10; the last 12 months are a locked holdout". That was a
  wording error in the prompt. Correct: stored data run to 2026-11; the locked holdout is 2025-11-10 → present; the
  development OOS is 2023-11 → 2025-11-10.
- "68 experiments recorded" vs "65 trials" is not a conflict: 68 counts every record (references, diagnostics,
  failed runs); the DSR trial count excludes those (`ExperimentRegistry.trials`).
- Q2 arithmetic re-computed and confirmed: days needed 7,474 / 2,971 / 1,925 for true annual Sharpe 0.5 / 0.8 / 1.0
  (z sum² = 4.508; V as stated); minimum detectable annual Sharpe ≈ 2.3 (1 year), ≈ 1.6 (2 years), ≈ 1.1 (4.25 years);
  on 2 years the "90% CI lower bound > 0" gate needs an observed Sharpe ≈ 1.25, passed ≈ 28% of the time by a true
  0.8. The expected maximum of 65 independent null Sharpes ≈ 2.37 cross-trial standard deviations (DSR formula).
- The reviewer's gate 1 (span ≥ MinTRL) and gate 3 (Sharpe CI) test the same condition; only one is kept in
  [validation-framework-v2.md](../validation-framework-v2.md).
- Gate 5 (effective-N DSR) cannot be applied retroactively: experiment records store summary metrics, not daily
  return series. Records written after framework v2 is approved store them.
- The operator's follow-up critique (four definitions missing, holdout role) is folded into framework v2 and the H1
  protocol rather than repeated here.

---

## Reviewer's text (verbatim)

**Desk state:** BINANCE_DEMO, live locked, kill switch released by the operator at 00:17Z, 0 positions, 0 intents,
no strategy passing gates (ev_bb986d4fb86e4f66ba0e, ev_d6c9193e695941c09e0e, ev_87a8816e1c7549b3851b).

**Read this first: the evidence is thinner than the brief asks for.**
- **No paper was read in full.** The backend fetcher rejects PDFs, two key pages returned 403, and I stopped at the
  desk's research budget (8 searches, 40 tool calls).
- **Labels used.** *Background* means abstract, summary page or search snippet retrieved this session. *Recalled*
  means from my training memory and not retrieved, so any number must be checked before it is cited. *Claim* means
  secondary or blog.
- **Embedded instructions.** One flag fired on the Man Group page ("You are now leaving…", site boilerplate); ignored.
- **Two inconsistencies in the brief.** Data "up to 2025-11-10" with a 12-month holdout overlaps the development
  window 2023-11 to 2025-11. The backend reports 68 recorded experiments, not 65.

### 1. Source table

| ID | Citation | URL | What it claims | Label | Limitations |
|---|---|---|---|---|---|
| S1 | Liu & Tsyvinski, RFS 2021 | doi 10.1093/rfs/hhaa113 | Market-level time-series momentum: current return predicts returns up to eight weeks ahead (as characterised by S4). 2011–2018, BTC/ETH/XRP and market index, daily/weekly, no costs. | Recalled + snippet | Sample ends 2018; gross |
| S2 | Liu, Tsyvinski & Wu, JF 2022 | doi 10.1111/jofi.13119 | Cross-sectional momentum factor on 1–4 week lookbacks; weekly; gross. | Recalled | Long-short, not implementable on spot long/flat |
| S3 | Borri, Liu, Tsyvinski & Wu, arXiv 2510.14435 (Oct 2025) | https://arxiv.org/html/2510.14435v1 | CoinGecko, 16,468 coins incl. delisted, weekly, 2014 to Sep 2025. 2-week cross-sectional momentum, value-weighted 5–1: 2.6%/week (t 3.89) full sample, 2.1%/week (t 3.70) post-2020. MOM12 and MOM24 not significant. | Background (HTML, ~94% read by extraction tool) | Costs not stated; no time-series momentum update in the text read; working paper |
| S4 | Han, Kang & Ryu, working paper; possibly published as Han, RAPS 2026 | acfr.aut.ac.nz PDF; academic.oup.com/raps (both 403) | With costs and daily price paths, many momentum strategies fail. Profit comes from the long leg and large coins; most coins show reversal. Time-series momentum is market-state dependent. RAPS snippet: time-series evidence "strong", cost assumption "15" (unit truncated). | Background (snippets only) | Effect sizes not retrieved; link between the two versions unverified |
| S5 | FFA Working Paper 5:003 (2023), "Impact of size and volume on cryptocurrency momentum and reversal" | quantitative.cz PDF | Large liquid coins: 1- and 2-week momentum (NW t 2.33, 2.68); beyond 4 weeks insignificant reversal (t −1.91 at 26 weeks). | Background (snippet) | Authors, sample, costs not retrieved |
| S6 | Zarattini, Pagani & Barbon, SSRN 5209907 (2025) | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907 | Donchian ensemble, volatility sizing, top-20 liquid coins since 2015, survivorship-free: net Sharpe > 1.5, alpha 10.8%/yr vs BTC. | Claim (authors' summary pages) | Not peer reviewed; lookbacks, costs, drawdown not retrieved; long-only in a rising market |
| S7 | Gbadebo, Vilnius Univ. journal 2026 | journals.vu.lt/BATP | Time-series vs cross-sectional momentum, multi-horizon EMA, 2020-01 to 2025-10. | Lead only | Results not retrieved |
| S8 | QuantSeeker recap of Xu & Wu | quantseeker.com/p/weekly-research-recap-a23 | 2018–2025: smallest quintile 4.9%/week reversal, largest +1.0%/week momentum. | Claim | Blog summary |
| S9 | Lo, FAJ 2002 | — | Sharpe ratio standard error; serial-correlation correction. | Recalled | — |
| S10 | Bailey & López de Prado, J. Risk 2012 | — | Probabilistic Sharpe Ratio and Minimum Track Record Length. | Recalled | — |
| S11 | Bailey & López de Prado, JPM 2014 | davidhbailey.com/dhbpapers/deflated-sharpe.pdf | Deflated Sharpe Ratio; N is the number of *independent* trials. | Background (snippet) | PDF not parsed |
| S12 | López de Prado, JFDS 2019; López de Prado & Lewis, Quant. Finance 2019 | AQR-hosted PDF | Cluster trial returns (ONC) to estimate effective trials. | Recalled | PDF downloaded, not parsed |
| S13 | White 2000; Hansen 2005; Romano & Wolf 2005; Harvey, Liu & Zhu, RFS 2016 | — | Bootstrap max-statistic tests and multiple-testing haircuts under dependence. | Recalled | — |
| S14 | Quantt blog | quantt.co.uk/resources/sharpe-ratio-of-pure-noise | 275 moving-average variants, mean pairwise correlation 0.65, about 10 effective trials. | Claim | Blog simulation |
| S15 | Kaminski & Lo, J. Financial Markets 2014 | — | Stop-loss lowers expected return under a random walk and can add value when returns have momentum. | Recalled + snippet in S16 | Equities/bonds, monthly |
| S16 | Clare, Seaton, Smith & Thomas, "Trend following, stop losses and the frequency of trading" | openaccess.city.ac.uk/id/eprint/17842 | Stops on top of trend rules cause whipsaw and cost drag. | Background (snippet) | S&P 500 only; numbers not retrieved |
| S17 | Korgaonkar (Man AHL), June 2025 | https://www.man.com/insights/is-this-time-different | Studies the opportunity cost of "deallocate at 20% drawdown, re-enter when the drawdown is made back" on a simulated Sharpe-0.6 strategy. | Claim (practitioner; text truncated) | Result numbers not retrieved; vendor of trend funds |
| S18 | Grossman & Zhou, Math. Finance 1993 | — | Under a drawdown constraint the optimal exposure is proportional to the cushion above the floor. | Recalled | Theory; continuous trading |
| S19 | Harvey et al., JPM 2018, "The Impact of Volatility Targeting" | man.com/insights/the-impact-of-volatility-targeting | 60+ assets: volatility scaling raises Sharpe for equities and credit, negligible for bonds, FX, commodities; reduces left tail and maximum drawdown. | Background (abstract) | No crypto |
| S20 | Moreira & Muir, JF 2017; Cederburg et al., JFE 2020 | — | Volatility-managed portfolios raise Sharpe in-sample; out-of-sample gains are not robust. | Recalled | Equity factors |
| S21 | Hurst, Ooi & Pedersen, JPM 2017 | — | Trend following 1880–2016, 67 markets, positions sized to equal volatility, 10% portfolio volatility target. | Recalled + snippet | No crypto |
| S22 | Alexander & Dakos, Quant. Finance 2020 | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3382828 | Many crypto papers used non-traded aggregate prices; results change with traded exchange data. | Recalled (page blocked) | — |
| S23 | Cong, Li, Tang & Yang, Management Science 2023 | — | 29 exchanges, mid-2019: wash trades above 70% of reported volume on unregulated exchanges on average. | Recalled | Exchange-level figure for Binance not verified; Binance denies (Business Insider, claim) |
| S24 | Bitwise presentation to the SEC, 2019 | — | About 95% of reported BTC volume fake; Binance among the venues with real volume. | Recalled (claim) | Conflicts with S23 on Binance |
| S25 | Makarov & Schoar, JFE 2020; Griffin & Shams, JF 2020 | — | Large cross-exchange price gaps in 2017–2018; Tether flows associated with 2017 price rises. | Recalled | — |

### 2. Findings by question

#### Q1. Evidence update — confidence: low to medium

- **Cross-sectional, coin-level.** Short-horizon (1–2 week) momentum among large coins persists in data to September
  2025 with a modest decay: 2.6% to 2.1% per week gross (S3). Small coins reverse (S4, S5, S8). Longer lookbacks are
  not significant (S3, S5).
- **Time-series.** S4 is the only study found that applies costs to both types. Its snippets say many strategies fail
  once costs and daily paths are included, and that time-series momentum depends on market state. The RAPS snippet
  calls time-series evidence "strong". I could not retrieve effect sizes.
- **Index-level persistence after 2018.** Not found. The authors' own 2025 update (S3) reports no time-series momentum
  result in the text I read.
- **Net-of-cost daily trend on large caps.** The only number found is S6 (Sharpe > 1.5), a non-peer-reviewed claim
  on a top-20 rotational universe, not on four coins.
- **Verdict on Liu & Tsyvinski.** "Persisted, decayed or regime-dependent" cannot be settled from what I retrieved.
  The best-supported reading is regime-dependent (S4), with weak support.
- **Open questions.** Effect sizes in S4; whether any study isolates BTC/ETH time-series momentum after 2021 net of
  costs; whether a long/flat rule beats exposure-matched buy-and-hold.

#### Q2. Evaluating low-frequency strategies — confidence: high on the arithmetic, medium on the inputs

**The information in a track record depends on its length in years and the true Sharpe, not on how it is sliced.**
Approximately, t ≈ SR_annual × √years / √V.

- **Trade-count threshold.** It is a proxy for span. With 20–40 right-skewed trend trades, a t-test on trade returns
  is unreliable. Keep it only as a concentration check.
- **Daily-return tests.** Block bootstrap and HAC t-statistics are the right unit, but they add no information beyond
  the span.
- **Pooling across assets.** Effective breadth is N / (1 + (N−1)ρ). Four coins at ρ ≈ 0.75 give about 1.2
  independent bets (illustrative ρ, not measured here). Pool with time-clustered errors and do not count trades across
  coins as independent.
- **Minimum track record length (S10).** MinTRL = 1 + [1 − γ₃·SR + ((γ₄−1)/4)·SR²] × (z_α / (SR − SR*))², in
  per-period units.

**Power formula.** Days needed to detect a true Sharpe at one-sided α with power 1−β:

T = V × (z_α + z_β)² / SR_d², where SR_d = SR_annual / √365 and V = [1 − γ₃·SR_d + ((γ₄−1)/4)·SR_d²] × (1 + 2Σρ_k)

**Worked number.** α = 10% gives z = 1.2816; power 80% gives z = 0.8416; the squared sum is 4.508. Assume skewness −1,
kurtosis 10 and AR(1) autocorrelation 0.05, so the serial factor is 1.105.

| True annual Sharpe | V | Days needed | Years |
|---|---|---|---|
| 0.5 | 1.136 | 7,474 | 20.5 |
| 0.8 | 1.156 | 2,971 | 8.1 |
| 1.0 | 1.170 | 1,925 | 5.3 |

- **Fat tails barely matter at daily frequency**, because they are multiplied by a tiny daily Sharpe. Serial
  correlation matters more.
- **Minimum detectable Sharpe** is about 2.3 for one year and 1.6 for two years.
- **Consequence for the current gate.** On a 2-year window the "CI lower bound > 0" gate needs an observed Sharpe of
  about 1.26. A strategy with true Sharpe 0.8 passes about 27% of the time (43% on four years). "Inconclusive" was the
  expected outcome of the daily test.
- **Consequence for the holdout and shadow log.** A 12-month holdout or one year of shadow data cannot confirm a
  Sharpe below about 2. It can only check consistency and implementation.
- **Open questions.** The measured autocorrelation and moments of the desk's daily strategy returns; these inputs are
  my assumptions.

#### Q3. Multiple testing — confidence: medium

- **Raw N is too harsh on the count.** The deflated Sharpe ratio assumes independent trials (S11). The expected
  maximum under the null is about 2.38 cross-trial standard deviations for N = 65, 1.90 for N = 20 and 1.57 for N = 10.
- **It is too lenient elsewhere.** Unregistered choices (universe picked with hindsight, bar size) are not counted,
  and near-duplicate trials understate the cross-trial variance. The net direction is unknown until computed.
- **Accepted ways to get effective N:** cluster the trial return correlation matrix and use the number of clusters and
  the variance of cluster Sharpes (S12); eigenvalue-based effective-number measures; or skip effective N and bootstrap
  the maximum statistic jointly over all trial return series (S13), which handles dependence directly but needs every
  trial's daily returns stored.
- **When a fresh sample is better.** When the trial count cannot be trusted or the window is burned, as here. The cost
  is power (Q2), so untouched *older* data is worth more than a short forward run.
- **Open question.** Whether daily return series for all registered trials are retained.

#### Q4. Risk policy vs trend following — confidence: low to medium; crypto-specific evidence not found

**(a) Hard drawdown stops**
- Stops reduce expected return when returns are a random walk and can help when returns have momentum (S15). On top of
  a trend rule they add whipsaw and costs (S16).
- Theory favours scaling exposure with the cushion above the floor over a binary stop (S18).
- Man AHL frames a drawdown rule with re-entry as an opportunity cost (S17); I did not retrieve its numbers.
- **My interpretation, not literature:** the desk is long/flat, so equity is constant when no position is open. An
  entry block at 10% below peak therefore cannot clear on its own. S17's re-entry rule needs a shadow equity curve for
  this reason. (Builder note: confirmed in code — there is no reset of the stored equity peak.)

**(b) Sizing**
- Fixed-fractional risk with an ATR stop *is* inverse-volatility sizing: notional = (risk% / k) × equity / ATR%. At
  0.5% risk and a 3×ATR stop, each position contributes roughly 0.17% of equity per day, about 3% annualised. Two
  correlated positions give about 5–6%.
- The literature standard is a 10% portfolio volatility target (S21). The desk already volatility-scales, at roughly
  half that level and without a portfolio-level correlation step.
- Volatility targeting helps Sharpe mainly for equities and credit and mainly cuts tails elsewhere (S19).
  Out-of-sample Sharpe gains are disputed (S20). Sizing changes scale and tail shape, not edge.
- **Crypto-specific.** No verified source on drawdown stops or volatility targeting for crypto trend strategies was
  found within budget.

#### Q5. Older data, 2017–2021 — confidence: medium on the list, low on magnitudes (mostly recalled)

- **Coverage.** Binance BTCUSDT and ETHUSDT start in August 2017, BNBUSDT in late 2017 and SOLUSDT in 2020 (recalled).
  An extension is therefore a two-coin test on a different universe.
- **Volume quality.** Wash trading inflated reported volume in this period (S23, S24). The sources conflict on Binance
  specifically. Candle prices are less affected than volume, but volume-based slippage assumptions are unreliable.
- **Price quality.** Cross-exchange gaps of several percent occurred in 2017–2018 (S25); S3 puts the 10th/90th
  percentile discount vs Kraken at −1.8%/+3.7% for 2014–2025. USDT is the quote asset and left its peg at times, which
  moves BTCUSDT without a BTC move (recalled).
- **Outages.** A multi-day Binance halt in February 2018 and degraded service during March 2020 and May 2021 (recalled;
  no official source fetched). Expect candle gaps exactly when stops would trigger.
- **Regime.** Two of the largest bull markets fall in this window. Any long exposure earns in them, so results must be
  compared with exposure-matched buy-and-hold. The 13 bps cost assumption is unverified for 2017–2018 spreads.

### 3. Candidate hypotheses (reviewer's proposals)

- **H1. Frozen-rule back-extension.** Unchanged daily TSMOM (EMA 20/100, 90-day momentum) and Donchian 55/20, 3×ATR(14)
  stop, current risk policy, 13 and 26 bps; BTCUSDT and ETHUSDT from the first Binance candle to the start of the
  current dataset. Falsified if net return at 2× costs ≤ 0 or return minus exposure-matched buy-and-hold ≤ 0.
  → Specified in [protocols/daily-baselines-backext-v1.md](../protocols/daily-baselines-backext-v1.md).
- **H2. Weekly market-level time-series momentum.** Weekly decision at Sunday 00:00 UTC; hold BTC and ETH if the BTC
  28-day log return > 0. Deferred.
- **H3. Sizing comparison** (10% portfolio volatility target vs 0.5%-risk sizing, signal fixed). Touches the risk
  policy; separate operator decision.
- A Donchian ensemble (S6) was left out on purpose: claim-level source, several correlated trials on a burned window.

### 4. Reviewer's recommended evaluation rules (superseded by framework v2)

1. Replace "≥ 100 trades" with a span gate (MinTRL at 90%). 2. Keep a trade floor only as a concentration check
(≥ 30 trades, positive after removing the two best). 3. Keep the Sharpe CI gate on daily net returns, stating the
implied threshold. 4. Add a timing-skill gate vs exposure-matched buy-and-hold (p ≤ 0.10, circular shift of the
position series). 5. DSR on effective trials, raw N reported beside it. 6. Keep the 2× cost gate. 7. Holdout and
shadow log are consistency checks, duration pre-committed. 8. Add an explicit "inconclusive" tier.

Follow-up available: four PDFs (Harvey et al. 2018, López de Prado 2019, the FFA working paper, Clare et al.) were
downloaded in that session but not parsed.

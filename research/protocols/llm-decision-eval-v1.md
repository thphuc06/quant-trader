# Protocol: does the LLM analyst's view carry information? (llm-decision-eval-v1)

Status: **PREREGISTERED 2026-11-11**, committed before the forecast field, the runner or any forecast existed.
Operator choices: Sonnet at medium effort for the scheduled analyst sessions; no budget cap beyond a per-run safety
limit.

## Question

Before an LLM view is allowed to matter in a decision, does it predict anything? Specifically: are the analyst's
24-hour directional probabilities better than (a) the historical base rate and (b) the mechanical baseline's state at
the same moment?

## What is measured

- **Forecast.** Every scored decision report carries `forecast = {horizon_hours: 24, probability_up: p}`: the
  probability that the close of the last closed 1h bar at t0 + 24h is above the close of the last closed 1h bar at
  t0. A value of 0.5 means "no view".
- **t0 is the server's recording time** of the decision report, not the `as_of` the model writes. Reference and
  outcome prices come from stored Binance 1h candles, never from the model.
- **Primary sample:** scheduled headless trader sessions (`scripts/decision_eval.py`): one session per universe symbol
  per run, fixed prompt (its SHA-256 is recorded), `claude -p --model sonnet --effort medium`, PAPER mode, a separate
  data directory (`data/decision_eval/`), trader role and permissions; only `record_decision_report` is additionally
  allowed; every order-related tool stays denied. Runs are scheduled at a fixed time by the operator, so the sample is
  not selected by when someone chose to ask.
- **Secondary sample:** forecasts in decision reports from ordinary trader sessions. Reported separately, never pooled
  with the primary sample (the operator chooses when to ask, which can select on market conditions).
- **Ledger:** one JSON line per forecast in `research/decision-eval/forecasts.jsonl` (append-only, committed): run id,
  symbol, t0, p, decision, model, effort, prompt hash, code commit, decision id. Raw transcripts stay local under
  `reports/decision-eval/`.

## Scoring (fixed now)

1. **Brier score** B = (p − y)², y = 1 if the 24h outcome is up.
2. **Climatology** p_c: share of up 24h moves for that symbol over the 365 days before t0, computed from candles that
   closed before t0. Skill per forecast: S = B_c − B_llm (positive = LLM better).
3. **Mechanical comparison:** at t0, the `tsmom_trend_v1` 1h target from the shadow log (long ⇒ "up", flat ⇒ "not up").
   On forecasts where the LLM's direction (p > 0.5 vs p < 0.5; p = 0.5 excluded) disagrees with the mechanical call,
   report which was right, with counts. Descriptive, not a gate.
4. **Calibration:** reliability table in 0.1 bins, reported once there are ≥ 200 scored forecasts.

## Inference and decision

- Forecasts made on the same day are correlated across symbols, so the unit of resampling is the **calendar day**
  (all symbols of one run together). 24h horizons from daily runs do not overlap in time.
- **Primary claim, evaluated once after 180 scheduled run-days (≈ 720 forecasts):** "the LLM view carries
  information" only if the 90% day-block bootstrap interval of mean S lies above 0. Rough power (assumed spread of
  daily mean S ≈ 0.06): detectable mean skill ≈ 0.008, about a 3% improvement on a 0.25 Brier score.
- **Early stop for harm only:** from run-day 60, if the 90% interval of mean S lies entirely below 0, conclude the LLM's
  directional views are worse than the base rate, and its directional statements must not be used as decision
  evidence. No early claim of skill.
- **If the primary claim fails at day 180:** LLM directional views are treated as uninformative. Its role stays
  evidence gathering, risk checks and explanation, not prediction.
- **If it passes:** the result is still one model and one period; it allows using the probability as one cited input
  in decision reports, not trading on it. Any trading use needs its own protocol under framework v2.

## Limitations

Model versions change over months; the model id and effort are recorded per forecast and a change of model starts a new
sample. The forecast question is deliberately simple (direction over 24h) and says nothing about magnitude or costs. A
four-coin universe with high correlation gives far fewer independent observations than forecasts.

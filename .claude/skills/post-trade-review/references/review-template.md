# Post-trade review template

Pass the object below as the `review` argument of `submit_post_trade_review`. It is validated against
`trader.learning.PostTradeReview` (a test keeps this template valid). Unknown keys are rejected.

```json
{
  "intent_id": "int_00000000000000000000",
  "decision_quality": "acceptable",
  "process_quality": "good",
  "data_quality": "good",
  "model_quality": "not_assessable",
  "execution_quality": "good",
  "risk_quality": "good",
  "regime_fit": "poor",
  "what_was_known_at_decision_time": "The 1h regime was range/vol_high and no strategy was validated; the entry relied on a mechanical breakout level.",
  "counterfactual": "Waiting for the 4h regime to turn up would have skipped this entry.",
  "notes": "Slippage matched the 2 bps assumption; the stop filled 0.1% below the trigger.",
  "lesson": {
    "title": "Breakout entries in range regimes",
    "claim": "Long breakout entries taken while the 1h regime is labelled range tend to reach the stop before the target.",
    "scope": "BTCUSDT 1h, range regime",
    "contradicting_cases": []
  }
}
```

- Ratings: `good`, `acceptable`, `poor`, `not_assessable`.
- `what_was_known_at_decision_time`: 20–2000 characters. `counterfactual`: 10–1500. `notes`: up to 3000.
- `lesson` is optional (`null` or omit it). `title` 8–120, `claim` 20–1500, `scope` 5–300 characters.

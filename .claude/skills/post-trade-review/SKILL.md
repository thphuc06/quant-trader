---
name: post-trade-review
description: Review a closed trade against its original decision, risk validation, approvals and fills; separate process quality from outcome; optionally draft a candidate lesson.
argument-hint: "[intent_id]"
---

# Post-trade review

Intent: $ARGUMENTS

1. `get_trade_case` — decision report, validations, approvals, orders, fills, outcome, MFE/MAE, journal.
   If the trade is not closed yet, say so and stop: reviews are for closed trades.
2. Compare plan with reality: entry vs reference price (slippage), stop and size vs the risk report, costs,
   holding time, exit reason.
3. Judge **process and outcome separately**. A loss with a sound process is not a mistake; a win with a broken
   process is not a success. Rate each dimension `good`, `acceptable`, `poor` or `not_assessable`.
4. Reconstruct what was knowable at decision time (from the stored report, not from hindsight) and one realistic
   counterfactual.
5. Lesson (optional): only if the evidence supports a claim that can be tested. One trade is one sample; the lesson
   is stored as a **candidate** and changes no strategy, policy or skill. Name contradicting cases if any.
6. Show the draft to the operator, then call `submit_post_trade_review` with an object that follows
   [references/review-template.md](references/review-template.md) exactly.
7. Tell the operator the next step for a lesson: a registered experiment (`/strategy-research`), then
   `python -m trader lesson promote` — which only the operator can run.

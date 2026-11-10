# Knowledge base

A small research library with provenance. Git history is the review trail.

## Rules

1. Every document starts with YAML front matter (schema below). Files without an `id` are ignored by the retriever.
2. `status` decides how a document may be used:

   | status | meaning | may be cited as a rule? |
   |---|---|---|
   | `unverified` | a note or idea | no |
   | `candidate` | a hypothesis waiting for a test (all auto-drafted lessons start here) | no |
   | `reviewed` | checked by the operator against its sources | yes |
   | `approved` | reviewed **and** backed by an experiment | yes |
   | `deprecated` | kept for history, hidden from search | no |

3. Summaries are written in our own words with links. No copied articles.
4. No current market values in documents. Those come from MCP tools at decision time.
5. Claude may read everything here. In trader sessions it may only create files in `lessons_pending_review/` (through the
   `submit_post_trade_review` tool). Promotion is `python -m trader lesson promote`, run by the operator.

## Front matter

```yaml
---
id: kb-unique-id
title: Short title
type: playbook | market_mechanics | risk_playbook | execution_playbook | indicator_reference | model_card | lesson | glossary | note
status: unverified | candidate | reviewed | approved | deprecated
tags: [trend, btc]
created: 2026-11-10
updated: 2026-11-10
author: who or what drafted it (name the model if an LLM)
confidence: low | medium | high
applicability: assets / regimes / horizons where it applies
sources:
  - {title: ..., url: ..., published: ..., accessed: 2026-11-10}
limitations: what would make this wrong
related: [experiment ids, trade case ids]
---
```

## Layout

| Folder | Content |
|---|---|
| `source_registry/` | `sources.yaml`: domain → source type |
| `market_mechanics/` | How the venue and instruments work |
| `strategy_playbooks/` | One card per strategy: hypothesis, rules, failure modes, validation status |
| `indicator_reference/` | What each feature measures and what it overlaps with |
| `risk_playbooks/`, `execution_playbooks/` | Operating procedures |
| `model_cards/` | One card per trained model |
| `trade_casebook/` | Sanitised, reviewed trade cases worth keeping (the raw journal stays in the database) |
| `lessons_pending_review/` | Auto-drafted candidate lessons |
| `approved_lessons/` | Lessons promoted by the operator with an experiment as evidence |
| `glossary/` | Terms |

All seed documents were drafted by Claude (Fable 5.2) on 2026-11-10 and are marked `candidate` or `unverified` unless the
operator has reviewed them. Review them and change the status yourself.

# ADR 0004 — Knowledge as reviewed markdown; lessons are candidates until validated

Status: accepted · 2026-11-10

## Decision
- Knowledge lives in `knowledge/**/*.md` with YAML front matter (id, type, tags, sources with access dates,
  confidence, applicability, limitations, `status`). Git is the version history and the review trail.
- `status` ∈ `unverified | candidate | reviewed | approved | deprecated`. Search results always show it; skills forbid
  citing anything below `reviewed` as a rule.
- Retrieval: in-process keyword scoring over the files with filters (type, tag, status). No vector database, no
  embeddings.
- Trade history: an append-only, hash-chained journal in SQLite. A *trade case* is the ordered set of journal events
  for one intent.
- Learning loop: post-trade review → **candidate lesson** file in `knowledge/lessons_pending_review/` → a research
  hypothesis and experiment → human promotion via `python -m trader lesson promote` (TTY only, requires an experiment
  ID) → moved to `knowledge/approved_lessons/`.
- Nothing in the loop edits `configs/`, `src/`, `.claude/` or the risk policy.

## Why
- The corpus is tens of documents; keyword search is sufficient and has no model/version drift. Revisit above a few
  hundred documents or if retrieval misses are observed.
- A single trade is one sample. Automatic rule changes from outcomes are self-confirming overfitting.
- Process quality and outcome are reviewed separately so that a lucky bad decision is not reinforced.

## Consequences
- Synonym/semantic recall is weak; documents need good titles and tags.
- Promotion is slow by design.

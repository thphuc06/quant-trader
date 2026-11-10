# state/

Hand-off notes between Claude Code sessions. Small, human-readable, committed when useful.

| File | Written by | Read by |
|---|---|---|
| `session-summary.md` | Claude at the end of a session or before compaction (trader role may write here) | the next session at start-up (`/trading-desk`, SessionStart hook) |
| `plan.md` | builder sessions | whoever implements the next step (Claude or Codex) |

The authoritative record is the backend journal (SQLite, append-only, hash-chained). These notes only point to it:
cite decision ids, intent ids and evidence ids instead of copying numbers.

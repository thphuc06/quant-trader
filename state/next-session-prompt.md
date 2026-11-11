# Prompt for the next builder session

Start Claude Code in the repo root as a builder: in PowerShell `$env:TRADER_SESSION_ROLE="builder"; claude`
(check that the session banner says `Session role: BUILDER`). Paste everything below the line.

---

You are starting fresh on this repository. A previous session built most of what is here; do not assume anything it
concluded is right. Your job in this session is to understand the project on your own and work out what it should do
next. Nobody will tell you the answer.

## 1. Explore

Read whatever you need to form your own picture, at your own pace and in your own order. At minimum:

- `CLAUDE.md` (rules for every session) and `README.md`.
- `state/` (plan, session summary, handoff notes), `docs/`, `architecture/`, `research/` (including the protocols,
  reviews, research log and experiment records), `knowledge/`, `configs/`, `.claude/` (settings, hooks, skills,
  agents).
- The code under `src/trader/` and the tests under `tests/`. Read enough of it to judge quality yourself rather than
  trusting the documentation's description of it.
- `git log` for how the project got here.

Run the checks in the project venv (`.venv/Scripts/python.exe -m pytest -o addopts="" -q`, `ruff check src tests
scripts`, `mypy`) and report what you find. You may run read-only CLI commands and read-only MCP tools. Scheduled
jobs write to the operator's data directory every hour and every day: inspect their outputs, but do not delete or
rewrite anything under `data/`.

## 2. Form a view

Decide for yourself:

- What this project is actually for, and whether what has been built serves that.
- What works, what is weak, what is wrong or risky, and what is missing. Look for problems the previous session did
  not notice.
- Where the evidence stands, and how much of it you would trust.
- What the most valuable next steps are, and why. Consider alternatives, including stopping or changing direction.

## 3. Report before building

Give the operator a report in Vietnamese: your understanding, your findings (with file and line references), and a
ranked proposal for what to do next with the reasoning and trade-offs. Then wait for the operator to choose. Do not
start implementing a direction before they agree.

## Rules that still apply

- Follow `CLAUDE.md`. Do not enable live trading, edit `configs/risk_policy.yaml`, or run human-only commands
  (approve, live-unlock, kill-switch release, lesson promote, `--unlock-holdout`, keygen).
- Never read `.env` or private key files.
- Ask the operator before anything that costs money, needs keys, registers or changes scheduled tasks, or pushes to
  the remote.

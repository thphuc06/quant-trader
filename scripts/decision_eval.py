#!/usr/bin/env python3
"""Scheduled LLM decision evaluation (research/protocols/llm-decision-eval-v1.md). See src/trader/evals/decisions.py.

Usage (from the repo root, with the project venv):
    .venv/Scripts/python scripts/decision_eval.py                      # all universe symbols, sonnet, medium effort
    .venv/Scripts/python scripts/decision_eval.py --symbols BTCUSDT --label smoke

Each symbol runs one fresh headless trader session in PAPER mode with a separate data directory. Spends Claude quota.
Score with: python -m trader decision-eval-score (no Claude calls).
"""

from trader.evals.decisions import main

if __name__ == "__main__":
    raise SystemExit(main())

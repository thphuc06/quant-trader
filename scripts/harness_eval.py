#!/usr/bin/env python3
"""Run the Claude Code harness eval. See src/trader/evals/harness.py.

Usage (from the repo root, with the project venv):
    .venv/Scripts/python scripts/harness_eval.py --label baseline            # all scenarios, haiku, $0.5 cap each
    .venv/Scripts/python scripts/harness_eval.py --only S2 S3 --label retry

This spends Claude quota. It is not part of pytest.
"""

from trader.evals.harness import main

if __name__ == "__main__":
    raise SystemExit(main())

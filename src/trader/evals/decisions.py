"""LLM decision evaluation (research/protocols/llm-decision-eval-v1.md).

Runner: one fresh headless trader session per universe symbol (``claude -p``), PAPER mode, trader role, a separate data
directory, and only ``record_decision_report`` allowed beyond the project's read-only allow list. Each recorded
forecast is appended to an append-only ledger. The runner spends Claude quota; it is not part of pytest.

Scorer: no Claude calls. Reference and outcome prices come from stored candles at the server's recording time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trader.evals.harness import parse_stream, project_allow_rules
from trader.quant.research import code_commit
from trader.timeutil import interval_ms, iso, to_ms

PROTOCOL_PATH = "research/protocols/llm-decision-eval-v1.md"
LEDGER = Path("research") / "decision-eval" / "forecasts.jsonl"
DATA_DIRNAME = "decision_eval"
RECORD_TOOL = "mcp__trading__record_decision_report"
GATE_SERVER = "eval_gate"
PROMPT_TEMPLATE = (
    "/analyze-symbol {symbol}\n\n"
    "This is a scheduled evaluation run in PAPER mode. Follow the skill fully and finish by recording the decision "
    "report, including the `forecast` field. Do not create, execute or cancel trade intents. Nobody will answer "
    "questions during this run; if a tool needs permission it will be denied, so continue without it."
)
HARM_CHECK_FROM_DAY = 60
EVALUATION_DAY = 180


def prompt_for(symbol: str) -> str:
    return PROMPT_TEMPLATE.format(symbol=symbol)


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- runner


def extract_forecast(lines: list[str]) -> dict[str, Any]:
    """The last successfully recorded decision report in a stream-json transcript, as the server echoed it."""
    transcript = parse_stream(lines)
    attempts = [c for c in transcript.calls if c.name == RECORD_TOOL]
    base: dict[str, Any] = {"cost_usd": transcript.cost_usd, "num_turns": transcript.num_turns,
                            "record_attempts": len(attempts)}  # fmt: skip
    for call in reversed(attempts):
        try:
            envelope = json.loads(call.result_text)
        except ValueError:
            continue
        data = envelope.get("data") or {}
        if envelope.get("status") == "ok" and data.get("stored"):
            forecast = data.get("forecast") or {}
            if forecast.get("probability_up") is None:
                return {**base, "status": "no_forecast", "decision_id": data.get("decision_id"),
                        "decision": data.get("decision")}  # fmt: skip
            return {
                **base,
                "status": "ok",
                "decision_id": data.get("decision_id"),
                "decision": data.get("decision"),
                "recorded_at": envelope.get("as_of"),
                "probability_up": float(forecast["probability_up"]),
                "horizon_hours": int(forecast.get("horizon_hours", 24)),
            }
    return {**base, "status": "not_recorded" if not attempts else "rejected"}


def append_ledger(root: Path, row: dict[str, Any]) -> None:
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_ledger(root: Path) -> list[dict[str, Any]]:
    path = root / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_session(
    symbol: str, *, root: Path, model: str, effort: str, budget_usd: float, out_dir: Path, timeout: int = 1800
) -> dict[str, Any]:
    data_dir = root / "data" / DATA_DIRNAME
    if not (data_dir / "candles").exists() and (root / "data" / "candles").exists():
        shutil.copytree(root / "data" / "candles", data_dir / "candles")  # warm public-data cache only
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    env.update(TRADER_SESSION_ROLE="trader", TRADING_MODE="PAPER", LIVE_TRADING_ENABLED="false",
               TRADER_DATA_DIR=str(data_dir))  # fmt: skip
    settings = {"env": {k: env[k] for k in ("TRADER_SESSION_ROLE", "TRADING_MODE", "LIVE_TRADING_ENABLED",
                                            "TRADER_DATA_DIR")}}  # fmt: skip
    allow = [*project_allow_rules(root), RECORD_TOOL]
    # The project's `ask` rule outranks --allowedTools, so the one write these sessions may make is answered by a
    # permission gate that allows only record_decision_report and denies every other prompt (see permission_gate.py).
    gate = {
        "mcpServers": {
            GATE_SERVER: {"command": sys.executable, "args": ["-m", "trader.evals.permission_gate"]}
        }
    }
    command = [
        shutil.which("claude") or "claude", "-p", prompt_for(symbol),
        "--model", model, "--effort", effort,
        "--output-format", "stream-json", "--verbose",
        "--max-turns", "40", "--max-budget-usd", f"{budget_usd:g}",
        "--settings", json.dumps(settings), "--allowedTools", ",".join(allow),
        "--mcp-config", json.dumps(gate), "--permission-prompt-tool", f"mcp__{GATE_SERVER}__decide",
    ]  # fmt: skip
    started = datetime.now(UTC)
    completed = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )  # fmt: skip
    (out_dir / f"{symbol}.jsonl").write_text(completed.stdout, encoding="utf-8")
    if completed.stderr.strip():
        (out_dir / f"{symbol}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    result = extract_forecast(completed.stdout.splitlines())
    result["exit_code"] = completed.returncode
    result["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 1)
    return result


def run(
    root: Path, symbols: list[str], *, model: str, effort: str, budget_usd: float, label: str,
    sample: str = "scheduled",
) -> list[dict]:  # fmt: skip
    """``sample``: 'scheduled' (the protocol's primary sample, from the operator's scheduled task) or 'manual'
    (ad-hoc runs such as smoke tests; recorded but never scored as primary)."""
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + (f"-{label}" if label else "")
    out_dir = root / "reports" / "decision-eval" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for symbol in symbols:
        try:
            result = run_session(
                symbol, root=root, model=model, effort=effort, budget_usd=budget_usd, out_dir=out_dir
            )
        except Exception as exc:  # a failed session is still a ledger row (not scored)
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        row = {
            "run_id": run_id, "sample": sample, "symbol": symbol, "run_started_at": iso(started),
            "model": model, "effort": effort, "prompt_sha256": prompt_sha256(), "code_commit": code_commit(root),
            "protocol": PROTOCOL_PATH, **result,
        }  # fmt: skip
        append_ledger(root, row)
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- scorer


def climatology(closes: pd.Series, close_times: pd.Series, t0_ms: int, horizon_bars: int) -> float | None:
    """Share of up moves over ``horizon_bars`` among 1h bars that closed in the 365 days before t0 (causal)."""
    mask = (close_times < t0_ms) & (close_times >= t0_ms - 365 * 86_400_000)
    window = closes[mask].to_numpy(dtype=float)
    if len(window) <= horizon_bars + 100:
        return None
    ups = window[horizon_bars:] > window[:-horizon_bars]
    return float(ups.mean())


def score_forecast(row: dict[str, Any], candles: pd.DataFrame, now_ms: int, step_ms: int) -> dict[str, Any]:
    t0 = to_ms(datetime.fromisoformat(str(row["recorded_at"]).replace("Z", "+00:00")))
    horizon_ms = int(row.get("horizon_hours", 24)) * 3_600_000
    if now_ms < t0 + horizon_ms:
        return {"state": "pending"}
    close_times = candles["close_time"].astype("int64")
    before = candles[close_times < t0]
    at_horizon = candles[close_times <= t0 + horizon_ms]
    if before.empty or at_horizon.empty:
        return {"state": "outcome_missing"}
    ref, out = before.iloc[-1], at_horizon.iloc[-1]
    if (
        int(out["close_time"]) <= t0 + horizon_ms - 2 * step_ms
    ):  # a gap: the bar that should close the horizon is absent
        return {"state": "outcome_missing"}
    y = 1.0 if float(out["close"]) > float(ref["close"]) else 0.0
    p = float(row["probability_up"])
    p_c = climatology(candles["close"], close_times, t0, horizon_ms // step_ms)
    if p_c is None:
        return {"state": "no_climatology"}
    return {
        "state": "scored",
        "y": y,
        "brier_llm": (p - y) ** 2,
        "brier_climatology": (p_c - y) ** 2,
        "skill": (p_c - y) ** 2 - (p - y) ** 2,
        "p": p,
        "p_climatology": p_c,
        "reference_bar_open": iso(datetime.fromtimestamp((int(ref["close_time"]) + 1 - step_ms) / 1000, UTC)),
    }


def day_block_ci(
    scored: pd.DataFrame, n_boot: int = 2000, ci: float = 0.90, seed: int = 7
) -> list[float] | None:
    """Bootstrap the mean skill by resampling whole run days (all symbols of a day together)."""
    days = scored.groupby("day")["skill"].agg(["sum", "count"])
    if len(days) < 20:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(days), size=(n_boot, len(days)))
    sums, counts = days["sum"].to_numpy()[idx].sum(axis=1), days["count"].to_numpy()[idx].sum(axis=1)
    means = sums / counts
    return [float(np.quantile(means, (1 - ci) / 2)), float(np.quantile(means, 1 - (1 - ci) / 2))]


def score(root: Path, candles_by_symbol: dict[str, pd.DataFrame], mechanical: dict[str, dict[int, float]],
          now_ms: int, interval: str = "1h", operator_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """mechanical[symbol][bar_open_ms] = tsmom target logged in the shadow log for that bar.
    operator_rows: forecasts from ordinary trader sessions (secondary sample), in ledger-row shape."""
    step = interval_ms(interval)
    ledger = read_ledger(root)
    rows = []
    for row in [*ledger, *(operator_rows or [])]:
        entry = {"run_id": row.get("run_id"), "sample": row.get("sample"), "symbol": row.get("symbol"),
                 "status": row.get("status"), "model": row.get("model")}  # fmt: skip
        if row.get("status") == "ok" and row.get("symbol") in candles_by_symbol:
            entry.update(score_forecast(row, candles_by_symbol[row["symbol"]], now_ms, step))
            entry["day"] = str(row.get("run_started_at", ""))[:10]
            if entry.get("state") == "scored":
                bar = to_ms(datetime.fromisoformat(str(entry["reference_bar_open"]).replace("Z", "+00:00")))
                entry["mechanical_long"] = mechanical.get(row["symbol"], {}).get(bar)
        rows.append(entry)
    frame = pd.DataFrame(rows)
    report: dict[str, Any] = {"protocol": PROTOCOL_PATH, "ledger_rows": len(ledger)}
    if frame.empty:
        return {**report, "status": "no_data"}
    report["by_status"] = frame["status"].value_counts().to_dict()
    states = frame.get("state")
    report["by_state"] = states.value_counts().to_dict() if states is not None else {}
    scored_mask = frame["state"].eq("scored") if "state" in frame else pd.Series(False, index=frame.index)
    for sample in ("scheduled", "operator"):
        part = frame[(frame["sample"] == sample) & scored_mask]
        days = int(part["day"].nunique()) if not part.empty else 0
        block: dict[str, Any] = {"scored": len(part), "run_days": days}
        if not part.empty:
            block.update({
                "mean_brier_llm": float(part["brier_llm"].mean()),
                "mean_brier_climatology": float(part["brier_climatology"].mean()),
                "mean_skill": float(part["skill"].mean()),
                "skill_ci90_day_block": day_block_ci(part),
                "share_no_view": float((part["p"] == 0.5).mean()),
            })  # fmt: skip
            dis = part[part["mechanical_long"].notna() & (part["p"] != 0.5)]
            llm_up = dis["p"] > 0.5
            mech_up = dis["mechanical_long"] > 0
            disagree = dis[llm_up != mech_up]
            block["disagreements_with_mechanical"] = {
                "count": len(disagree),
                "llm_right": int(((disagree["p"] > 0.5) == (disagree["y"] == 1.0)).sum()),
                "mechanical_right": int(((disagree["mechanical_long"] > 0) == (disagree["y"] == 1.0)).sum()),
            }
            if len(part) >= 200:
                bins = pd.cut(part["p"], [i / 10 for i in range(11)], include_lowest=True)
                block["reliability"] = [
                    {"bin": str(b), "count": int(g["y"].count()), "mean_p": float(g["p"].mean()),
                     "observed_up": float(g["y"].mean())}
                    for b, g in part.groupby(bins, observed=True)
                ]  # fmt: skip
        if sample == "scheduled":
            block["protocol_stage"] = (
                "evaluation due" if days >= EVALUATION_DAY
                else "harm check active" if days >= HARM_CHECK_FROM_DAY
                else f"collecting ({days}/{EVALUATION_DAY} run-days; descriptive only)"
            )  # fmt: skip
            ci = block.get("skill_ci90_day_block")
            block["harm_signal"] = bool(days >= HARM_CHECK_FROM_DAY and ci is not None and ci[1] < 0)
            block["primary_claim_supported"] = (
                bool(ci[0] > 0) if days >= EVALUATION_DAY and ci is not None else None
            )
        report[sample] = block
    report["status"] = "ok"
    return report


def _nan_to_none(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _nan_to_none(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nan_to_none(v) for v in value]
    return value


def main(argv: list[str] | None = None) -> int:
    import argparse

    from trader.desk import TradingDesk

    parser = argparse.ArgumentParser(description="LLM decision evaluation (spends Claude quota)")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--effort", default="medium")
    parser.add_argument(
        "--budget-usd", type=float, default=3.0, help="per-session safety cap (--max-budget-usd)"
    )
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--sample", choices=["scheduled", "manual"], default="manual",
        help="'scheduled' only from the operator's scheduled task (primary sample); anything else is 'manual'",
    )  # fmt: skip
    args = parser.parse_args(argv)
    desk = TradingDesk()
    rows = run(desk.root, args.symbols or desk.app.symbols, model=args.model, effort=args.effort,
               budget_usd=args.budget_usd, label=args.label, sample=args.sample)  # fmt: skip
    for row in rows:
        print(json.dumps({k: row.get(k) for k in ("symbol", "status", "decision", "probability_up", "cost_usd",
                                                    "seconds", "exit_code")}))  # fmt: skip
    return 0 if all(r.get("status") == "ok" for r in rows) else 1


def score_main(desk: Any) -> dict[str, Any]:
    """Scorer entry used by the CLI: refreshes 1h candles and reads shadow-log mechanical states."""
    from trader.shadow import all_rows

    interval = "1h"
    candles, mechanical = {}, {}
    for symbol in desk.app.symbols:
        frame, _ = desk.market.candles(
            symbol, interval, refresh=True, min_bars=desk.app.bars_needed(interval)
        )
        candles[symbol] = frame
        mechanical[symbol] = {
            int(r["open_time_ms"]): float(r["strategies"]["tsmom_trend_v1"]["target"])
            for r in all_rows(desk.db, symbol, interval)
            if r.get("bar_status") == "closed" and r.get("strategies")
        }
    operator_rows = []
    for record in desk.db.query(
        "SELECT decision_id, symbol, decision, payload_json, created_at FROM decisions"
    ):
        forecast = json.loads(record["payload_json"]).get("forecast")
        if forecast and forecast.get("probability_up") is not None:
            operator_rows.append({
                "run_id": None, "sample": "operator", "symbol": record["symbol"], "status": "ok",
                "decision_id": record["decision_id"], "decision": record["decision"],
                "recorded_at": record["created_at"], "run_started_at": record["created_at"],
                "probability_up": float(forecast["probability_up"]), "horizon_hours": int(forecast.get("horizon_hours", 24)),
            })  # fmt: skip
    return _nan_to_none(
        score(desk.root, candles, mechanical, to_ms(datetime.now(UTC)), interval, operator_rows)
    )

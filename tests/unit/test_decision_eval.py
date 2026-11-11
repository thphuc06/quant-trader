"""LLM decision evaluation: forecast extraction from transcripts, causal scoring, ledger aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import synthetic_candles
from trader.domain import DecisionReport
from trader.evals import decisions
from trader.timeutil import from_ms, iso

H = 3_600_000
END = 1_794_000_000_000 - (1_794_000_000_000 % H)
RECORD = decisions.RECORD_TOOL


def stream(*results: tuple[str, dict]) -> list[str]:
    lines = []
    for i, (status, data) in enumerate(results):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": RECORD, "input": {"report": {}}}]}}))  # fmt: skip
        envelope = {"status": status, "as_of": "2026-11-11T07:00:05Z", "evidence_id": "ev_x", "data": data}
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": [{"type": "text", "text": json.dumps(envelope)}]}]}}))  # fmt: skip
    lines.append(json.dumps({"type": "result", "result": "done", "total_cost_usd": 0.42, "num_turns": 9}))
    return lines


def test_forecast_is_read_from_the_servers_echo_of_the_last_stored_report() -> None:
    ok = {"decision_id": "dec_1", "decision": "WATCH", "stored": True,
          "forecast": {"horizon_hours": 24, "probability_up": 0.46}}  # fmt: skip
    got = decisions.extract_forecast(stream(("rejected", {"reason": "schema"}), ("ok", ok)))
    assert got["status"] == "ok" and got["probability_up"] == 0.46 and got["decision_id"] == "dec_1"
    assert (
        got["recorded_at"] == "2026-11-11T07:00:05Z"
        and got["record_attempts"] == 2
        and got["cost_usd"] == 0.42
    )
    assert decisions.extract_forecast(stream(("rejected", {"reason": "schema"})))["status"] == "rejected"
    assert decisions.extract_forecast(stream())["status"] == "not_recorded"
    no_view = {"decision_id": "dec_2", "decision": "WATCH", "stored": True, "forecast": None}
    assert decisions.extract_forecast(stream(("ok", no_view)))["status"] == "no_forecast"


def test_forecast_field_is_validated() -> None:
    base = {"as_of": "2026-11-11T07:00:00Z", "symbol": "BTCUSDT", "horizon": "24h", "decision": "WATCH"}
    assert (
        DecisionReport.model_validate({**base, "forecast": {"probability_up": 0.55}}).forecast.horizon_hours
        == 24
    )  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        DecisionReport.model_validate({**base, "forecast": {"probability_up": 1.5}})


def test_scoring_uses_candles_at_the_recording_time_and_a_causal_climatology() -> None:
    candles = synthetic_candles(24 * 400, end_ms=END, seed=5)
    t0 = int(candles["close_time"].iloc[-30]) + 60_000  # recorded one minute after a bar closed
    row = {"recorded_at": iso(from_ms(t0)), "probability_up": 0.7, "horizon_hours": 24}
    assert decisions.score_forecast(row, candles, now_ms=t0 + 3 * H, step_ms=H)["state"] == "pending"
    scored = decisions.score_forecast(row, candles, now_ms=END, step_ms=H)
    ref_close = float(candles["close"].iloc[-30])
    out_close = float(candles["close"].iloc[-6])  # last bar closing at or before t0 + 24h
    assert scored["y"] == (1.0 if out_close > ref_close else 0.0)
    assert scored["brier_llm"] == pytest.approx((0.7 - scored["y"]) ** 2)
    # the base rate must not change when the future is removed
    truncated = candles.iloc[:-29]
    p_c = decisions.climatology(truncated["close"], truncated["close_time"].astype("int64"), t0, 24)
    assert scored["p_climatology"] == pytest.approx(p_c)


def test_score_aggregates_by_day_and_compares_with_the_mechanical_state(tmp_path: Path) -> None:
    candles = synthetic_candles(24 * 420, end_ms=END, seed=6)
    times = candles["close_time"].astype("int64")
    ledger_rows, mechanical = [], {}
    for day in range(25):
        t0 = int(times.iloc[-24 * (30 - day)]) + 60_000
        ledger_rows.append({"run_id": f"r{day}", "sample": "scheduled", "symbol": "BTCUSDT", "status": "ok",
                            "recorded_at": iso(from_ms(t0)), "run_started_at": iso(from_ms(t0)),
                            "probability_up": 0.6 if day % 2 else 0.4, "horizon_hours": 24})  # fmt: skip
        mechanical[t0 - 60_000 + 1 - H] = 1.0  # bar open of the reference bar: mechanical says long
    ledger_rows.append(
        {"run_id": "bad", "sample": "scheduled", "symbol": "BTCUSDT", "status": "not_recorded"}
    )
    for row in ledger_rows:
        decisions.append_ledger(tmp_path, row)
    report = decisions.score(tmp_path, {"BTCUSDT": candles}, {"BTCUSDT": mechanical}, now_ms=END)
    block = report["scheduled"]
    assert block["scored"] == 25 and block["run_days"] == 25
    assert block["skill_ci90_day_block"] is not None and block["harm_signal"] is False
    assert block["protocol_stage"].startswith("collecting")
    dis = block["disagreements_with_mechanical"]
    # p = 0.4 on the 13 even days (0, 2, ..., 24) disagrees with the mechanical "long"; exactly one side is right
    assert dis["count"] == 13 and dis["llm_right"] + dis["mechanical_right"] == 13
    assert report["by_status"] == {"ok": 25, "not_recorded": 1}
    assert report["operator"]["scored"] == 0


def test_prompt_hash_is_stable_and_the_prompt_forbids_trading() -> None:
    assert decisions.prompt_sha256() == decisions.prompt_sha256()
    prompt = decisions.prompt_for("ETHUSDT")
    assert (
        prompt.startswith("/analyze-symbol ETHUSDT")
        and "Do not create, execute or cancel trade intents" in prompt
    )


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__trading__create_trade_intent", "mcp__trading__execute_approved_trade", "mcp__trading__cancel_trade_intent",
        "mcp__trading__activate_kill_switch", "mcp__trading__sync_positions", "mcp__trading__run_backtest",
        "mcp__trading__submit_post_trade_review", "Bash", "Write", "Edit", "WebFetch",
    ],
)  # fmt: skip
def test_permission_gate_denies_everything_but_the_decision_report(tool: str) -> None:
    from trader.evals.permission_gate import decision_for

    assert decision_for(tool, {"x": 1})["behavior"] == "deny"
    allowed = decision_for("mcp__trading__record_decision_report", {"report": {"symbol": "BTCUSDT"}})
    assert allowed == {"behavior": "allow", "updatedInput": {"report": {"symbol": "BTCUSDT"}}}


@pytest.mark.anyio
async def test_permission_gate_answers_over_mcp() -> None:
    from mcp import Client

    from trader.evals.permission_gate import gate

    async with Client(gate, raise_exceptions=True) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {"decide"}
        result = await client.call_tool(
            "decide", {"tool_name": "mcp__trading__execute_approved_trade", "input": {}}
        )
        assert json.loads(result.content[0].text)["behavior"] == "deny"  # type: ignore[union-attr]
        # regression (smoke run 2): Claude Code rejects anything but a single text block, so no structured wrapper
        assert len(result.content) == 1 and not getattr(result, "structured_content", None)

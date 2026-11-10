"""MCP server contract (in-memory client), secret hygiene, bounded outputs, and the human-only CLI gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client
from pydantic import SecretStr

from tests.conftest import FakeSource, decision_payload, make_desk
from trader import cli, mcp_server
from trader.config import TradingMode

SECRETS = [
    "live-key-AAAA1111",
    "live-secret-BBBB2222",
    "demo-key-CCCC3333",
    "demo-secret-DDDD4444",
    "tavily-EEEE5555",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(repo_copy: Path, source: FakeSource):
    desk = make_desk(
        repo_copy, source, TradingMode.PAPER, binance_live_api_key=SecretStr(SECRETS[0]), binance_live_api_secret=SecretStr(SECRETS[1]),
        binance_demo_api_key=SecretStr(SECRETS[2]), binance_demo_api_secret=SecretStr(SECRETS[3]), tavily_api_key=SecretStr(SECRETS[4]),
    )  # fmt: skip
    mcp_server.set_desk(desk)
    yield desk
    mcp_server.set_desk(None)


EXPECTED_TOOLS = {
    "get_system_status", "get_market_overview", "get_market_snapshot", "get_ohlcv", "get_derivatives_snapshot", "get_strategy_signals",
    "get_model_predictions", "run_backtest", "get_experiment_results", "search_web", "search_crypto_news", "fetch_web_page", "search_knowledge",
    "get_knowledge_document", "get_pending_lessons", "get_account_state", "get_open_positions", "sync_positions", "get_risk_state", "calculate_position_risk",
    "get_trade_intent", "get_trade_case", "record_decision_report", "submit_post_trade_review", "create_trade_intent", "execute_approved_trade",
    "cancel_trade_intent", "activate_kill_switch",
}  # fmt: skip
FORBIDDEN_FRAGMENTS = [
    "withdraw",
    "transfer",
    "deposit",
    "approve_",
    "release",
    "unlock",
    "set_risk",
    "update_policy",
    "leverage",
    "margin",
    "signed_request",
    "shell",
    "api_key",
    "secret",
    "place_order",
    "promote",
]


@pytest.mark.anyio
async def test_tool_surface_is_exactly_the_documented_one(server) -> None:
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS
        for name in names:
            assert not any(
                fragment in name for fragment in FORBIDDEN_FRAGMENTS if name != "execute_approved_trade"
            ), name
        by_name = {t.name: t for t in tools}
        assert all(t.description and len(t.description) > 40 for t in tools)
        # the intent tool has no way to pass a quantity or a raw payload
        assert not {"quantity", "qty", "amount", "payload", "url"} & set(
            by_name["create_trade_intent"].input_schema["properties"]
        )
        assert by_name["get_market_snapshot"].annotations.read_only_hint is True
        assert by_name["execute_approved_trade"].annotations.read_only_hint is False

        resources = {str(r.uri) for r in (await client.list_resources()).resources}
        assert {
            "trading://system/status",
            "trading://risk-policy/current",
            "trading://execution/mode",
            "trading://schemas/decision-report",
        } <= resources
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {
            "daily_market_briefing",
            "analyze_symbol",
            "evaluate_trade_setup",
            "post_trade_review",
        }


@pytest.mark.anyio
async def test_full_flow_over_mcp_never_leaks_secrets_and_outputs_are_bounded(server) -> None:
    outputs: list[str] = []
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:

        async def call(name: str, args: dict | None = None) -> dict:
            result = await client.call_tool(name, args or {})
            text = json.dumps(result.structured_content)
            outputs.append(text)
            assert len(text) < 60_000, f"{name} returned {len(text)} chars"
            return result.structured_content

        status = await call("get_system_status")
        assert status["data"]["trading_mode"] == "PAPER" and status["evidence_id"].startswith("ev_")
        await call("get_market_overview")
        snapshot = await call("get_market_snapshot", {"symbol": "BTCUSDT"})
        assert set(snapshot["data"]["timeframes"]) == {"1h", "4h", "1d"}
        candles = await call("get_ohlcv", {"symbol": "BTCUSDT", "interval": "1h", "limit": 5000})
        assert len(candles["data"]["rows"]) <= 200
        assert (await call("get_ohlcv", {"symbol": "BTCUSDT", "interval": "1m"}))["status"] == "rejected"
        await call("get_derivatives_snapshot", {"symbol": "ETHUSDT"})
        await call("get_strategy_signals", {"symbol": "BTCUSDT"})
        assert (await call("get_model_predictions", {"symbol": "BTCUSDT"}))["status"] == "unavailable"
        assert (await call("search_web", {"query": "bitcoin"}))[
            "status"
        ] == "unavailable"  # provider list is empty in tests
        assert (await call("fetch_web_page", {"url": "http://127.0.0.1:22/"}))["status"] == "rejected"
        assert (await call("get_market_snapshot", {"symbol": "DOGEUSDT"}))["status"] == "rejected"
        await call("search_knowledge", {"query": "trend"})
        await call("get_risk_state")
        await call("get_account_state")
        preview = await call("calculate_position_risk", {"symbol": "BTCUSDT", "stop_price": "98"})
        assert preview["data"]["would_be_approved"] is True

        report = decision_payload(server)
        decision = await call("record_decision_report", {"report": report})
        intent = await call(
            "create_trade_intent",
            {
                "decision_id": decision["data"]["decision_id"],
                "symbol": "BTCUSDT",
                "side": "BUY",
                "stop_price": "98",
                "rationale": "mcp integration test entry with stop",
                "idempotency_key": "mcp-key-00001",
            },
        )
        intent_id = intent["data"]["intent"]["intent_id"]
        executed = await call("execute_approved_trade", {"intent_id": intent_id})
        assert executed["data"]["intent_status"] == "FILLED"
        await call("get_open_positions")
        synced = await call("sync_positions")
        assert "settled" in synced["data"] and len(synced["data"]["positions"]) == 1
        await call("get_trade_intent", {"intent_id": intent_id})
        await call("get_trade_case", {"intent_id": intent_id})
        assert (await call("submit_post_trade_review", {"review": {"intent_id": intent_id}}))[
            "status"
        ] == "rejected"
        assert (await call("activate_kill_switch", {"reason": "test"}))["data"]["engaged"] is True
        await call("get_experiment_results")
        await call("get_pending_lessons")

        for uri in (
            "trading://system/status",
            "trading://risk-policy/current",
            "trading://execution/mode",
            "trading://universe/active",
            "trading://strategies/active",
            "trading://knowledge/index",
        ):
            contents = (await client.read_resource(uri)).contents
            outputs.append(contents[0].text)
        prompt = await client.get_prompt("analyze_symbol", {"symbol": "BTCUSDT"})
        assert "BTCUSDT" in prompt.messages[0].content.text

    blob = "\n".join(outputs)
    for secret in SECRETS:
        assert secret not in blob
    for row in server.db.query("SELECT args_json, summary FROM audit_events"):
        assert not any(secret in row["args_json"] + row["summary"] for secret in SECRETS)
    assert server.db.query("SELECT COUNT(*) AS n FROM audit_events")[0]["n"] >= 20  # every call is audited


@pytest.mark.anyio
async def test_backend_failure_becomes_an_error_envelope_not_a_guess(server, source: FakeSource) -> None:
    source.fail_quote = True
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        result = (await client.call_tool("get_market_snapshot", {"symbol": "BTCUSDT"})).structured_content
        assert result["status"] == "unavailable" and "quote" not in result["data"]
        overview = (await client.call_tool("get_market_overview", {})).structured_content
        assert overview["status"] == "unavailable"


# ----------------------------------------------------------------------------- CLI gates


@pytest.mark.parametrize(
    "argv",
    [
        ["approve", "int_x"],
        ["kill-switch", "release"],
        ["live-unlock"],
        ["lesson", "promote", "lesson_x", "--experiment", "exp_x"],
        ["backtest", "no_trade", "--unlock-holdout"],
    ],
)
def test_human_only_commands_refuse_inside_claude_code(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert "REFUSED" in str(exc.value)


def test_human_only_commands_refuse_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    with pytest.raises(SystemExit) as exc:  # pytest's stdin is not a TTY
        cli.require_human("approve")
    assert "interactive terminal" in str(exc.value)


def test_confirmation_must_match_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "btcusdt")
    with pytest.raises(SystemExit):
        cli.confirm("BTCUSDT", "approve?")
    monkeypatch.setattr("builtins.input", lambda _prompt: "BTCUSDT")
    cli.confirm("BTCUSDT", "approve?")

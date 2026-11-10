"""Operator CLI:  python -m trader <command>

Human-only commands (approve, kill-switch release, live-unlock, lesson promote) refuse to run without an
interactive terminal and refuse to run inside a Claude Code tool shell. This stops the assistant's own
tools; it is not a hard security boundary against other software running as the same OS user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

from trader.config import TradingMode
from trader.desk import TradingDesk
from trader.domain import jsonable
from trader.timeutil import iso, utc_now


def _print(value: Any) -> None:
    print(json.dumps(jsonable(value), indent=2, default=str))


def require_human(action: str) -> None:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        raise SystemExit(
            f"REFUSED: '{action}' must be run by the operator in their own terminal, not from Claude Code."
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(f"REFUSED: '{action}' needs an interactive terminal (no pipes, no scripts).")


def confirm(expected: str, prompt: str) -> None:
    typed = input(f"{prompt}\nType exactly '{expected}' to confirm: ").strip()
    if typed != expected:
        raise SystemExit("Confirmation did not match. Nothing was changed.")


# ----------------------------------------------------------------------------- commands


def cmd_doctor(args: argparse.Namespace) -> int:
    """Offline health check: config, policy, database, MCP server import, hooks."""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Any) -> None:
        try:
            checks.append((name, True, str(fn())))
        except Exception as exc:
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    desk_box: dict[str, TradingDesk] = {}

    def make_desk() -> str:
        desk_box["d"] = TradingDesk()
        return f"root={desk_box['d'].root} mode={desk_box['d'].settings.trading_mode.value}"

    check("config and database", make_desk)
    if "d" in desk_box:
        d = desk_box["d"]
        check(
            "risk policy",
            lambda: f"v{d.execution.policy().policy.version} ({d.execution.policy().policy.status})",
        )
        check(
            "journal integrity",
            lambda: (
                d.db.journal_verify()[1]
                if d.db.journal_verify()[0]
                else (_ for _ in ()).throw(RuntimeError(d.db.journal_verify()[1]))
            ),
        )
        check(
            "live trading locked",
            lambda: (
                "locked: " + "; ".join(d.execution.live_gate()[1])
                if not d.execution.live_gate()[0]
                else "UNLOCKED"
            ),
        )
        check("knowledge base", lambda: f"{d.knowledge.index()['documents']} documents")
        check(
            "gitignore covers .env",
            lambda: (
                "ok"
                if ".env" in (d.root / ".gitignore").read_text(encoding="utf-8")
                else (_ for _ in ()).throw(RuntimeError(".env not ignored"))
            ),
        )

    def mcp_tools() -> str:
        from trader import mcp_server

        async def count() -> int:
            from mcp import Client

            async with Client(mcp_server.mcp) as client:
                return len((await client.list_tools()).tools)

        return f"{asyncio.run(count())} tools registered"

    check("mcp server", mcp_tools)
    if "d" in desk_box:
        hooks = sorted((desk_box["d"].root / ".claude" / "hooks").glob("*.py"))
        check(
            "hook scripts compile",
            lambda: f"{len([compile(h.read_text(encoding='utf-8'), str(h), 'exec') for h in hooks])} hooks",
        )
    if args.network and "d" in desk_box:
        check(
            "binance public market data",
            lambda: f"BTCUSDT mid {desk_box['d'].market.source.quote('BTCUSDT').mid}",
        )
    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_status(args: argparse.Namespace) -> int:
    _print(TradingDesk().system_status())
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    desk = TradingDesk()
    symbols = [args.symbol.upper()] if args.symbol else desk.app.symbols
    for symbol in symbols:
        for interval in args.intervals.split(","):
            frame = desk.market.backfill(symbol, interval, args.days)
            _, quality = desk.market.candles(symbol, interval, refresh=False)
            print(
                f"{symbol} {interval}: {len(frame)} bars, quality={quality.as_dict()['status']} {quality.issues}"
            )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    desk = TradingDesk()
    if args.unlock_holdout:
        require_human("unlock holdout")
        confirm("UNLOCK", "Evaluating the holdout spends it: it can never again be untouched confirmation.")
    symbols = [args.symbol.upper()] if args.symbol else desk.app.symbols
    for symbol in symbols:
        result = desk.run_backtest(symbol, args.strategy, unlock_holdout=args.unlock_holdout)
        data = result["data"]
        if result["status"] != "ok":
            print(f"{symbol}: {result['status']} {data}")
            continue
        dev = data["development"]
        print(
            f"{symbol} {args.strategy} [{data['experiment_id']}] dev: return={dev['net_total_return']:.3f} sharpe={dev['sharpe']} "
            f"maxDD={dev['max_drawdown']:.3f} trades={dev['trades']} sharpeCI={dev['sharpe_ci_annualized']} "
            f"DSR={data['selection_bias']['deflated_sharpe_ratio']} gates={data['gates_provisional']} holdout={data['holdout']['status']}"
        )
    return 0


def cmd_research_volatility_overlay(args: argparse.Namespace) -> int:
    from trader.quant.research import evaluate_volatility_overlay, record_volatility_overlay_family
    from trader.storage.candles import check_quality
    from trader.timeutil import interval_ms

    desk = TradingDesk()
    symbols = [args.symbol.upper()] if args.symbol else desk.app.symbols
    strategies = [args.strategy] if args.strategy else ["tsmom_trend_v1", "donchian_breakout_v1"]
    failures = 0
    results = []
    result_qualities: dict[str, dict] = {}
    rebalance_band = float(getattr(args, "rebalance_band", 0.0))
    daily_rebalance = bool(getattr(args, "daily_rebalance", False))
    for symbol in symbols:
        try:
            interval = desk.app.decision_timeframe
            candles = desk.market.store.load(symbol, interval)
            if candles.empty:
                candles = desk.market.backfill(
                    symbol, interval, int(desk.app.backtest.get("history_days", 1825))
                )
            if candles.empty:
                raise ValueError("no locally stored candles; ingest public data first")
            quality = check_quality(
                candles,
                symbol,
                interval,
                now_ms=int(candles["open_time"].max()) + interval_ms(interval),
            )
        except Exception as exc:
            failures += 1
            for strategy in strategies:
                experiment_id = desk.registry.record(
                    "volatility_overlay",
                    {"status": "failed", "symbol": symbol, "base_strategy": strategy,
                     "reason": f"market data unavailable: {type(exc).__name__}: {exc}",
                     "holdout": {"status": "locked"}},
                )
                print(f"{symbol} {strategy} [{experiment_id}] failed: market data unavailable")
            continue
        for strategy in strategies:
            try:
                result = evaluate_volatility_overlay(
                    candles,
                    symbol=symbol,
                    interval=desk.app.decision_timeframe,
                    base_strategy=strategy,
                    params=desk.app.strategies.get(strategy, {}),
                    costs=desk.app.costs,
                    holdout_start=str(desk.app.backtest["holdout_start"]),
                    validation=desk.app.validation,
                    registry=desk.registry,
                    root=desk.root,
                    train_days=int(desk.app.ml.get("train_days", 730)),
                    test_days=int(desk.app.backtest.get("walk_forward_test_days", 90)),
                    rebalance_band=rebalance_band,
                    daily_rebalance=daily_rebalance,
                    data_quality=quality.as_dict(),
                    record=False,
                )
            except Exception as exc:
                failures += 1
                experiment_id = desk.registry.record(
                    "volatility_overlay",
                    {"status": "failed", "symbol": symbol, "base_strategy": strategy,
                     "reason": str(exc), "data_quality": quality.as_dict(),
                     "holdout": {"status": "locked"}},
                )
                print(f"{symbol} {strategy} [{experiment_id}] failed: {exc}")
                continue
            results.append(result)
            result_qualities[result["symbol"]] = quality.as_dict()
    record_volatility_overlay_family(results, desk.registry, float(desk.app.validation.get("min_dsr", 0.5)))
    for result in results:
        base, overlay = result["baseline"], result["overlay"]
        print(
            f"{result['symbol']} {result['base_strategy']} [{result['experiment_id']}] "
            f"baseline Sharpe={base['sharpe']} overlay Sharpe={overlay['sharpe']} "
            f"return={overlay['net_total_return']:.3f} maxDD={overlay['max_drawdown']:.3f} "
            f"turnover/y={overlay['turnover_per_year']} gates={result['gates_provisional']} "
            f"data={result_qualities[result['symbol']]['status']}"
            + (
                f" policy_turnover_reduction={result['preregistered_policy_checks']['turnover_reduction_fraction']:.1%}"
                if result.get("preregistered_policy_checks")
                else ""
            )
        )
    return 1 if failures else 0


def cmd_research_volatility_band(args: argparse.Namespace) -> int:
    """Run the single preregistered 5 percentage-point no-trade band variant."""
    args.rebalance_band = 0.05
    return cmd_research_volatility_overlay(args)


def cmd_research_volatility_daily(args: argparse.Namespace) -> int:
    """Run the single preregistered daily volatility-resizing variant."""
    args.daily_rebalance = True
    return cmd_research_volatility_overlay(args)


def cmd_research_turnover_attribution(args: argparse.Namespace) -> int:
    """Replay the baseline and hourly volatility overlay as a diagnostic, not a strategy trial."""
    from trader.quant.research import evaluate_volatility_overlay
    from trader.storage.candles import check_quality
    from trader.timeutil import interval_ms

    desk = TradingDesk()
    symbols = [args.symbol.upper()] if args.symbol else desk.app.symbols
    strategies = [args.strategy] if args.strategy else ["tsmom_trend_v1", "donchian_breakout_v1"]
    failures = 0
    for symbol in symbols:
        try:
            interval = desk.app.decision_timeframe
            candles = desk.market.store.load(symbol, interval)
            if candles.empty:
                candles = desk.market.backfill(
                    symbol, interval, int(desk.app.backtest.get("history_days", 1825))
                )
            if candles.empty:
                raise ValueError("no locally stored candles; ingest public data first")
            quality = check_quality(
                candles,
                symbol,
                interval,
                now_ms=int(candles["open_time"].max()) + interval_ms(interval),
            )
        except Exception as exc:
            failures += 1
            for strategy in strategies:
                desk.registry.record(
                    "turnover_attribution",
                    {
                        "diagnostic_only": True,
                        "counts_toward_dsr": False,
                        "status": "failed",
                        "symbol": symbol,
                        "base_strategy": strategy,
                        "reason": f"market data unavailable: {type(exc).__name__}: {exc}",
                        "holdout": {"status": "locked"},
                    },
                )
            continue
        for strategy in strategies:
            try:
                result = evaluate_volatility_overlay(
                    candles,
                    symbol=symbol,
                    interval=interval,
                    base_strategy=strategy,
                    params=desk.app.strategies.get(strategy, {}),
                    costs=desk.app.costs,
                    holdout_start=str(desk.app.backtest["holdout_start"]),
                    validation=desk.app.validation,
                    registry=desk.registry,
                    root=desk.root,
                    train_days=int(desk.app.ml.get("train_days", 730)),
                    test_days=int(desk.app.backtest.get("walk_forward_test_days", 90)),
                    data_quality=quality.as_dict(),
                    record=False,
                )
                result.pop("_oos_returns", None)
                result.pop("selection_bias", None)
                result.pop("gates_provisional", None)
                result.pop("all_gates_passed", None)
                result.pop("preregistered_policy_checks", None)
                result.pop("hourly_overlay_reference", None)
                result.pop("unbanded_overlay_reference", None)
                result["diagnostic_only"] = True
                result["counts_toward_dsr"] = False
                result["diagnostic_kind"] = "turnover_attribution_replay"
                experiment_id = desk.registry.record("turnover_attribution", result)
            except Exception as exc:
                failures += 1
                experiment_id = desk.registry.record(
                    "turnover_attribution",
                    {
                        "diagnostic_only": True,
                        "counts_toward_dsr": False,
                        "status": "failed",
                        "symbol": symbol,
                        "base_strategy": strategy,
                        "reason": str(exc),
                        "data_quality": quality.as_dict(),
                        "holdout": {"status": "locked"},
                    },
                )
                print(f"{symbol} {strategy} [{experiment_id}] attribution replay failed: {exc}")
                continue
            print(
                f"{symbol} {strategy} [{experiment_id}] diagnostic only; "
                f"baseline turnover={result['baseline']['turnover_per_year_by_reason']} "
                f"overlay turnover={result['overlay']['turnover_per_year_by_reason']} "
                f"holdout={result['holdout']['status']}"
            )
    return 1 if failures else 0


def cmd_pbo(args: argparse.Namespace) -> int:
    """Probability of backtest overfitting over a small, pre-declared parameter family."""
    from trader.quant.backtest import run_backtest
    from trader.quant.research import holdout_start_ms
    from trader.quant.stats import pbo_cscv, sharpe_per_period
    from trader.quant.strategies import STRATEGIES

    desk = TradingDesk()
    symbol, interval = args.symbol.upper(), desk.app.decision_timeframe
    candles = desk.market.backfill(symbol, interval, int(desk.app.backtest.get("history_days", 1825)))
    candles = candles[
        candles["open_time"] < holdout_start_ms(str(desk.app.backtest["holdout_start"]))
    ].reset_index(drop=True)
    if args.strategy == "tsmom_trend_v1":
        family = [
            {"ema_fast": f, "ema_slow": s, "mom_lookback": s, "atr_stop_mult": 3.0}
            for f in (12, 24, 48)
            for s in (96, 168, 336)
        ]
    elif args.strategy == "donchian_breakout_v1":
        family = [
            {"entry_lookback": e, "exit_lookback": e // 2, "atr_stop_mult": m}
            for e in (48, 96, 168, 336)
            for m in (2.0, 3.0, 4.0)
        ]
    else:
        raise SystemExit("pbo supports tsmom_trend_v1 and donchian_breakout_v1")
    cost = desk.app.costs.per_side_fraction()
    columns = [
        run_backtest(candles, STRATEGIES[args.strategy](candles, p), cost).returns.fillna(0.0).to_numpy()
        for p in family
    ]
    matrix = np.column_stack(columns)
    result = pbo_cscv(matrix, n_splits=10)
    sharpes = [sharpe_per_period(c) for c in columns]
    experiment_id = desk.registry.record(
        "pbo",
        {"symbol": symbol, "interval": interval, "strategy": args.strategy, "family": family, "config_sharpes_per_period": sharpes, "result": result,
         "note": "Each configuration counts as a trial for DSR.", "holdout": {"status": "locked"}},
    )  # fmt: skip
    _print({"experiment_id": experiment_id, "configurations": len(family), **result})
    return 0


def cmd_train_model(args: argparse.Namespace) -> int:
    result = TradingDesk().train_model(args.symbol.upper(), args.model)
    data = result["data"]
    if result["status"] != "ok":
        _print(result)
        return 1
    ev = data["evaluation"]
    _print(
        {
            k: ev[k]
            for k in (
                "model",
                "symbol",
                "folds",
                "oos_samples",
                "log_loss",
                "log_loss_base_rate",
                "brier",
                "brier_base_rate",
                "roc_auc",
                "ece",
                "gates_provisional",
                "out_of_sample_status",
                "net_return_by_probability_bucket",
            )
        }
        | {"experiment_id": data["experiment_id"]}
    )
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from trader.exchanges.binance.stream import KlineCollector, stream_url

    desk = TradingDesk()
    url = stream_url(desk.app.endpoints.market_data_ws, desk.app.symbols, desk.app.decision_timeframe)

    def backfill(_symbol: str, _interval: str) -> None:
        for symbol in desk.app.symbols:
            desk.market.refresh(symbol, desk.app.decision_timeframe)

    collector = KlineCollector(url, desk.market.store, backfill)

    async def run() -> None:
        task = asyncio.create_task(collector.run())
        if args.seconds:
            await asyncio.sleep(args.seconds)
            collector.stop()
        await task

    print(
        f"collecting closed {desk.app.decision_timeframe} klines for {desk.app.symbols}; Ctrl+C to stop",
        file=sys.stderr,
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        collector.stop()
    print(f"messages={collector.messages} stored={collector.stored} reconnects={collector.reconnects}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    require_human("approve")
    desk = TradingDesk()
    details = desk.execution.intent_details(args.intent_id)
    if details is None:
        raise SystemExit("unknown intent")
    intent = details["intent"]
    request = intent["request"]
    report = details["creation_report"] or {}
    print(f"\nMODE: {intent['mode']}   (current process mode: {desk.settings.trading_mode.value})")
    print(f"Intent {intent['intent_id']}  status={intent['status']}  expires={intent['expires_at']}")
    print(
        f"  {request['side']} {intent['quantity']} {request['symbol']}  type={request['order_type']}  limit={request['limit_price']}"
    )
    print(f"  stop={request['stop_price']}  target={request['take_profit']}")
    sizing = report.get("sizing") or {}
    print(
        f"  notional~{sizing.get('notional')}  worst-case loss~{sizing.get('worst_case_loss')}  (policy v{report.get('policy_version')})"
    )
    print(f"  rationale: {request['rationale']}\n")
    if desk.settings.trading_mode is TradingMode.LIVE:
        print("  *** LIVE MODE: REAL FUNDS ***\n")
    confirm(request["symbol"], "Approve this exact order? The backend will re-validate it at execution time.")
    approval = desk.execution.record_cli_approval(args.intent_id)
    print(
        f"Approved until {approval['expires_at']}. Single use. Now ask Claude to call execute_approved_trade."
    )
    return 0


def cmd_kill_switch(args: argparse.Namespace) -> int:
    desk = TradingDesk()
    if args.action == "status":
        _print(desk.execution.kill_switch())
    elif args.action == "engage":
        _print(
            desk.execution.engage_kill_switch(
                args.reason or "operator", by="operator_cli", cancel_open_orders=args.cancel_pending
            )
        )
    else:
        require_human("kill-switch release")
        confirm("RELEASE", "Release the kill switch and allow new positions again?")
        _print(desk.execution.release_kill_switch(by="operator_cli_tty"))
    return 0


def cmd_live_unlock(args: argparse.Namespace) -> int:
    require_human("live-unlock")
    desk = TradingDesk()
    policy = desk.execution.policy().policy
    print(
        "Live trading requires ALL of: TRADING_MODE=LIVE, LIVE_TRADING_ENABLED=true, live credentials, risk policy status 'approved', and this unlock."
    )
    confirm(
        "I ACCEPT THE RISK OF REAL LOSSES",
        "This unlock lets approved intents send REAL orders with REAL funds.",
    )
    expires = utc_now() + timedelta(minutes=policy.live_unlock_ttl_minutes)
    path = desk.data_dir / "live_unlock.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created_at": iso(utc_now()), "expires_at": iso(expires)}), encoding="utf-8")
    desk.db.journal_append("system", "live_unlock_created", {"expires_at": iso(expires)})
    ok, reasons = desk.execution.live_gate()
    print(
        f"Unlock written, expires {iso(expires)}. Live gate now: {'OPEN' if ok else 'still closed: ' + '; '.join(reasons)}"
    )
    return 0


def cmd_lesson(args: argparse.Namespace) -> int:
    desk = TradingDesk()
    if args.action == "list":
        _print(desk.pending_lessons()["data"]["lessons"])
        return 0
    require_human("lesson promote")
    if desk.registry.get(args.experiment) is None:
        raise SystemExit(f"experiment {args.experiment} not found; a lesson needs validation evidence")
    confirm(args.lesson_id, "Promote this lesson to approved knowledge?")
    doc = desk.knowledge.promote_lesson(args.lesson_id, args.experiment, reviewer="operator")
    desk.db.journal_append(
        "system", "lesson_promoted", {"lesson_id": doc.doc_id, "experiment_id": args.experiment}
    )
    print(f"Promoted -> knowledge/{doc.path}. Commit the file to record the review.")
    return 0


def cmd_verify_journal(args: argparse.Namespace) -> int:
    ok, message = TradingDesk().db.journal_verify()
    print(("OK: " if ok else "FAILED: ") + message)
    return 0 if ok else 1


def cmd_backup(args: argparse.Namespace) -> int:
    desk = TradingDesk()
    target = Path(args.to) if args.to else desk.data_dir / "backups"
    target.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    destination = target / f"trader-{stamp}.sqlite"
    import sqlite3

    with sqlite3.connect(desk.db.path) as source, sqlite3.connect(destination) as dest:
        source.backup(dest)
    print(f"database backed up to {destination}")
    if shutil.which("git") is None:
        print("note: git not found; knowledge and experiments are versioned in git")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from trader.mcp_server import main as run_server

    run_server()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader", description="Claude Trader operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="health check (offline unless --network)")
    p.add_argument("--network", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    sub.add_parser("status", help="system status as JSON").set_defaults(fn=cmd_status)

    p = sub.add_parser("ingest", help="download candle history")
    p.add_argument("--symbol")
    p.add_argument("--intervals", default="1h,4h,1d")
    p.add_argument("--days", type=int, default=1825)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("backtest", help="evaluate a strategy; every run is logged as a trial")
    p.add_argument("strategy")
    p.add_argument("--symbol")
    p.add_argument("--unlock-holdout", action="store_true")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser(
        "research-volatility-overlay",
        help="evaluate 30-day volatility-scaled baseline strategies with rolling walk-forward folds",
    )
    p.add_argument("--symbol")
    p.add_argument("--strategy", choices=["tsmom_trend_v1", "donchian_breakout_v1"])
    p.set_defaults(fn=cmd_research_volatility_overlay)

    p = sub.add_parser(
        "research-volatility-band",
        help="run the preregistered volatility target with a fixed 5 percentage-point no-trade band",
    )
    p.add_argument("--symbol")
    p.add_argument("--strategy", choices=["tsmom_trend_v1", "donchian_breakout_v1"])
    p.set_defaults(fn=cmd_research_volatility_band)

    p = sub.add_parser(
        "research-volatility-daily",
        help="run the preregistered daily-only volatility resizing variant",
    )
    p.add_argument("--symbol")
    p.add_argument("--strategy", choices=["tsmom_trend_v1", "donchian_breakout_v1"])
    p.set_defaults(fn=cmd_research_volatility_daily)

    p = sub.add_parser(
        "research-turnover-attribution",
        help="replay baseline and hourly volatility overlay with turnover/cost attribution; not a DSR trial",
    )
    p.add_argument("--symbol")
    p.add_argument("--strategy", choices=["tsmom_trend_v1", "donchian_breakout_v1"])
    p.set_defaults(fn=cmd_research_turnover_attribution)

    p = sub.add_parser("pbo", help="probability of backtest overfitting over a fixed parameter family")
    p.add_argument("strategy")
    p.add_argument("--symbol", required=True)
    p.set_defaults(fn=cmd_pbo)

    p = sub.add_parser("train-model", help="purged walk-forward evaluation, then fit and store a model")
    p.add_argument("--symbol", required=True)
    p.add_argument("--model", default="logreg_v1", choices=["logreg_v1", "hgb_v1"])
    p.set_defaults(fn=cmd_train_model)

    p = sub.add_parser("collect", help="optional websocket kline collector")
    p.add_argument("--seconds", type=int, default=0)
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("approve", help="HUMAN ONLY: approve one trade intent")
    p.add_argument("intent_id")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("kill-switch", help="status | engage | release (release is HUMAN ONLY)")
    p.add_argument("action", choices=["status", "engage", "release"])
    p.add_argument("--reason")
    p.add_argument("--cancel-pending", action="store_true")
    p.set_defaults(fn=cmd_kill_switch)

    sub.add_parser("live-unlock", help="HUMAN ONLY: time-limited unlock of live trading").set_defaults(
        fn=cmd_live_unlock
    )

    p = sub.add_parser("lesson", help="list | promote (promote is HUMAN ONLY)")
    p.add_argument("action", choices=["list", "promote"])
    p.add_argument("lesson_id", nargs="?")
    p.add_argument("--experiment")
    p.set_defaults(fn=cmd_lesson)

    sub.add_parser("verify-journal", help="recompute the journal hash chain").set_defaults(
        fn=cmd_verify_journal
    )

    p = sub.add_parser("backup", help="copy the SQLite database")
    p.add_argument("--to")
    p.set_defaults(fn=cmd_backup)

    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(fn=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "lesson" and args.action == "promote" and not (args.lesson_id and args.experiment):
        raise SystemExit("usage: trader lesson promote <lesson_id> --experiment <experiment_id>")
    return int(args.fn(args) or 0)

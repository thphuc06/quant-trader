"""Forward paper P&L of the signals recorded in the shadow log.

What it answers: "had the desk traded the logged baseline signals mechanically, under the risk policy, what would it
have made, and did it beat simply holding the same average exposure?" It uses only what the shadow log recorded at
each closed bar (targets and ATR) plus the stored candles for fills, so it is a forward measurement on data no
backtest was tuned on. It does not trade and does not involve the LLM.

What it cannot answer: whether any edge exists. A few months of hourly data detect only very large Sharpe ratios
(the report states the threshold for its length); early on it is a check for implementation errors and large losses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from trader.config import load_risk_policy
from trader.quant.framework_v2 import implied_sharpe_threshold
from trader.quant.portfolio import PortfolioLimits, daily_returns, portfolio_summary, run_portfolio_backtest
from trader.quant.research import cost_component_rates
from trader.quant.strategies import ACTIVE_STRATEGIES, StrategySignal
from trader.shadow import all_rows
from trader.timeutil import from_ms, interval_ms, iso

if TYPE_CHECKING:
    from trader.desk import TradingDesk

LATE_LOG_BARS = (
    2  # a row logged more than this many bars after its close was caught up, not seen in real time
)


def _logged_signals(rows: list[dict[str, Any]], strategy: str) -> pd.DataFrame:
    records = []
    for row in rows:
        if row.get("bar_status") != "closed":
            continue  # a missing bar carries no decision; the previous one stays in force
        strat = (row.get("strategies") or {}).get(strategy) or {}
        records.append(
            {
                "open_time": int(row["open_time_ms"]),
                "target": strat.get("target"),
                "atr": (row.get("features") or {}).get("atr"),
            }
        )
    return pd.DataFrame.from_records(records, columns=["open_time", "target", "atr"])


def evaluate_shadow(desk: TradingDesk, strategies: list[str] | None = None) -> dict[str, Any]:
    interval = desk.app.decision_timeframe
    step = interval_ms(interval)
    symbols = list(desk.app.symbols)
    rows = {s: all_rows(desk.db, s, interval) for s in symbols}
    if any(not r for r in rows.values()):
        return {"status": "unavailable", "reason": "some symbols have no shadow-log rows yet",
                "rows": {s: len(r) for s, r in rows.items()}}  # fmt: skip

    first_common = max(int(r[0]["open_time_ms"]) for r in rows.values())
    last_common = min(int(r[-1]["open_time_ms"]) for r in rows.values())
    if last_common - first_common < 2 * step:
        return {"status": "insufficient_data", "reason": "fewer than three logged bars common to all symbols",
                "first_common_bar": iso(from_ms(first_common)), "last_common_bar": iso(from_ms(last_common))}  # fmt: skip

    candles: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = desk.market.store.load(symbol, interval)
        # the first logged bar is the warm-up decision; the last fill uses the bar after the last logged decision
        frame = frame[(frame["open_time"] >= first_common) & (frame["open_time"] <= last_common + step)]
        candles[symbol] = frame.reset_index(drop=True)

    limits = PortfolioLimits.from_policy(load_risk_policy(desk.root).policy)
    all_logged = [
        row for r in rows.values() for row in r if first_common <= int(row["open_time_ms"]) <= last_common
    ]
    late = sum(
        1 for row in all_logged if (row.get("lag_seconds_after_close") or 0) > LATE_LOG_BARS * step / 1000
    )
    missing = sum(1 for row in all_logged if row.get("bar_status") == "missing")

    report: dict[str, Any] = {
        "status": "ok",
        "interval": interval,
        "first_decision_bar": iso(from_ms(first_common)),
        "last_decision_bar": iso(from_ms(last_common)),
        "logged_rows": len(all_logged),
        "rows_logged_late": late,
        "rows_missing": missing,
        "data_note": (
            "Late rows were computed from data closed at their bar but logged afterwards (catch-up); they are "
            "causal but were not seen in real time."
        ),
        "strategies": {},
    }
    for strategy in strategies or ACTIVE_STRATEGIES:
        params = desk.app.strategies.get(strategy, {})
        signals: dict[str, StrategySignal] = {}
        for symbol in symbols:
            logged = _logged_signals(rows[symbol], strategy).set_index("open_time")
            aligned = logged.reindex(candles[symbol]["open_time"].to_numpy())
            signals[symbol] = StrategySignal(
                strategy, "shadow", pd.Series(aligned["target"].to_numpy(dtype=float)),
                pd.Series(aligned["atr"].to_numpy(dtype=float)), float(params.get("atr_stop_mult", 3.0)), dict(params),
                pd.DataFrame(index=range(len(aligned))),
            )  # fmt: skip
        out: dict[str, Any] = {}
        for label, multiplier in (("base_costs", 1.0), ("costs_2x", 2.0)):
            result = run_portfolio_backtest(
                candles, signals, interval=interval, limits=limits,
                cost_components=cost_component_rates(desk.app.costs, multiplier),
                start_time_ms=first_common + step, priority=symbols,
            )  # fmt: skip
            summary = portfolio_summary(result)
            held = result.symbol_exposure.shift(1).fillna(0.0)
            mean_exposure = held.mean(axis=0)
            matched = float(np.prod(1.0 + (result.asset_returns * mean_exposure).sum(axis=1)) - 1.0)
            equal_weight = float(np.prod(1.0 + result.asset_returns.mean(axis=1)) - 1.0)
            days = len(daily_returns(result))
            out[label] = {
                "net_total_return": summary["net_total_return"],
                "max_drawdown": summary["max_drawdown"],
                "closed_trades": summary["trades"],
                "trades_open_at_end": summary["trades_open_at_end"],
                "mean_gross_exposure": summary["mean_gross_exposure"],
                "modeled_cost_bps_initial_equity": summary["modeled_cost_bps_initial_equity"]["total"],
                "buy_and_hold_at_same_mean_exposure": matched,
                "excess_vs_exposure_matched": summary["net_total_return"] - matched,
                "equal_weight_buy_and_hold_full_exposure": equal_weight,
                "entry_rejections": summary["entry_rejections"],
                "days": days,
            }
        report["strategies"][strategy] = out
    days = next(iter(report["strategies"].values()))["base_costs"]["days"]
    report["what_this_can_show"] = {
        "days_of_data": days,
        "observed_annual_sharpe_needed_for_significance": implied_sharpe_threshold(max(days, 1)),
        "reading": (
            "Until the needed Sharpe falls to realistic levels (years of data), treat this as a check for "
            "implementation errors and large losses, not as evidence of profitability."
        ),
    }
    return report

"""Small, transparent bar simulator. One symbol, long/flat, exposure in [0, 1].

Assumptions (all pessimistic or neutral, all stated in reports):
- A signal decided at the close of bar t is executed at the OPEN of bar t+1.
- Each traded notional pays ``cost_per_side`` (fee + half spread + slippage).
- A stop is fixed at entry: entry - stop_mult * ATR(signal bar). It is checked against the bar LOW from the
  entry bar onward. If the bar opens below the stop, the fill is the open (gap), not the stop.
- After a stop-out, re-entry waits until the signal has gone flat once.
- No partial fills, queue position, latency or market impact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isclose

import numpy as np
import pandas as pd

from trader.quant.strategies import StrategySignal

TURNOVER_REASONS = ("baseline_entry", "baseline_exit", "overlay_resize", "stop_touch", "stop_gap")


@dataclass
class Trade:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    net_return: float
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    strategy: str
    equity: pd.Series  # indexed by bar position, value at each bar close (starts at 1.0 before bar 0)
    returns: pd.Series  # per-bar net simple returns
    open_time: pd.Series
    in_position: pd.Series  # actual fraction of equity exposed to the asset
    turnover: pd.Series  # traded notional / equity immediately before each rebalance
    trades: list[Trade] = field(default_factory=list)
    cost_per_side: float = 0.0
    modeled_cost_initial_equity: float = 0.0
    turnover_by_reason: pd.DataFrame = field(default_factory=pd.DataFrame)
    modeled_costs_by_component_and_reason: pd.DataFrame = field(default_factory=pd.DataFrame)


def run_backtest(
    candles: pd.DataFrame,
    signal: StrategySignal,
    cost_per_side: float,
    rebalance_band: float = 0.0,
    rebalance_schedule_column: str | None = None,
    cost_components: dict[str, float] | None = None,
) -> BacktestResult:
    opens = candles["open"].to_numpy(dtype=float)
    lows = candles["low"].to_numpy(dtype=float)
    closes = candles["close"].to_numpy(dtype=float)
    times = candles["open_time"].to_numpy(dtype=np.int64)
    target = signal.target.to_numpy(dtype=float)
    atr_values = signal.atr.to_numpy(dtype=float)
    n = len(candles)

    if len(target) != n or len(atr_values) != n:
        raise ValueError("signal arrays must have the same length as candles")
    if not np.isfinite(cost_per_side) or cost_per_side < 0:
        raise ValueError("cost_per_side must be finite and non-negative")
    component_rates = dict(cost_components or {"all_in": cost_per_side})
    if (
        not component_rates
        or any(not np.isfinite(rate) or rate < 0 for rate in component_rates.values())
        or not isclose(sum(component_rates.values()), cost_per_side, rel_tol=1e-9, abs_tol=1e-12)
    ):
        raise ValueError("cost component rates must be non-negative and sum to cost_per_side")
    if not np.isfinite(rebalance_band) or not 0.0 <= rebalance_band < 1.0:
        raise ValueError("rebalance_band must be finite and in [0, 1)")
    if rebalance_schedule_column is not None and rebalance_schedule_column not in signal.detail:
        raise ValueError(f"signal detail has no rebalance schedule column {rebalance_schedule_column!r}")
    schedule = (
        signal.detail[rebalance_schedule_column].fillna(False).to_numpy(dtype=bool)
        if rebalance_schedule_column is not None
        else None
    )
    base_signal = (
        signal.detail["base_target"].fillna(0.0).to_numpy(dtype=float)
        if rebalance_schedule_column is not None and "base_target" in signal.detail
        else None
    )
    if np.any(~np.isfinite(target)) or np.any((target < 0.0) | (target > 1.0)):
        raise ValueError("target exposure must be finite and between 0 and 1")

    equity = np.ones(n)
    held = np.zeros(n)
    turnover = np.zeros(n)
    reason_turnover = np.zeros((n, len(TURNOVER_REASONS)), dtype=float)
    component_costs = np.zeros((n, len(component_rates) * len(TURNOVER_REASONS)), dtype=float)
    cost_columns = [f"{component}__{reason}" for component in component_rates for reason in TURNOVER_REASONS]
    trades: list[Trade] = []

    cash = 1.0
    modeled_cost_paid = 0.0
    units = 0.0
    entry_price = 0.0
    entry_equity = 0.0
    entry_index = -1
    stop = np.nan
    blocked = False  # after a stop-out, wait for the signal to reset

    def record_transaction(i: int, reason: str, notional: float, equity_before: float) -> None:
        nonlocal modeled_cost_paid
        reason_index = TURNOVER_REASONS.index(reason)
        turnover_ratio = notional / max(equity_before, 1e-15)
        reason_turnover[i, reason_index] += turnover_ratio
        turnover[i] += turnover_ratio
        modeled_cost_paid += notional * cost_per_side
        for component_index, rate in enumerate(component_rates.values()):
            column_index = component_index * len(TURNOVER_REASONS) + reason_index
            # The simulator starts at 1.0, so this is modeled cost as a fraction of initial equity.
            component_costs[i, column_index] += notional * rate

    def finish_position(i: int, price: float, reason: str, proceeds_booked: bool = False) -> None:
        nonlocal cash, units, stop
        closing_equity = cash if proceeds_booked else cash + units * price * (1.0 - cost_per_side)
        trades.append(
            Trade(
                int(times[entry_index]),
                int(times[i]),
                entry_price,
                price,
                closing_equity / entry_equity - 1.0,
                i - entry_index,
                reason,
            )
        )
        cash = closing_equity
        units = 0.0
        stop = np.nan

    for i in range(n):
        desired = target[i - 1] if i > 0 else 0.0
        baseline_flat = base_signal is not None and i > 0 and base_signal[i - 1] <= 0.0
        if desired == 0.0 or baseline_flat:
            desired = 0.0
            blocked = False
        if blocked:
            desired = 0.0

        open_equity = cash + units * opens[i]
        current_exposure = units * opens[i] / open_equity if open_equity > 0 else 0.0
        scheduled = schedule is None or i == 0 or bool(schedule[i - 1])
        if schedule is not None and units > 0.0 and desired > 0.0 and not scheduled:
            desired = current_exposure
        # A flat signal is a risk exit and always closes. While long, suppress small exposure adjustments.
        if desired > 0.0 and abs(desired - current_exposure) <= rebalance_band:
            desired = current_exposure
        if opens[i] <= 0 or open_equity <= 0:
            target_units = 0.0
        elif desired > current_exposure:
            target_units = desired * (open_equity + cost_per_side * units * opens[i]) / (
                opens[i] * (1.0 + desired * cost_per_side)
            )
        elif desired < current_exposure:
            target_units = desired * (open_equity - cost_per_side * units * opens[i]) / (
                opens[i] * (1.0 - desired * cost_per_side)
            )
        else:
            target_units = units
        had_position = units > 0.0
        delta_units = target_units - units
        if abs(delta_units) > 1e-15:
            notional = abs(delta_units * opens[i])
            if not had_position and target_units > 0.0:
                reason = "baseline_entry"
            elif had_position and target_units == 0.0:
                reason = "baseline_exit"
            else:
                reason = "overlay_resize"
            record_transaction(i, reason, notional, open_equity)
            cash -= delta_units * opens[i] + notional * cost_per_side
            if units == 0.0 and target_units > 0.0:
                entry_equity = open_equity
                entry_price = opens[i]
                entry_index = i
            prior_atr = atr_values[i - 1]
            if units == 0.0 and target_units > 0.0:
                stop = (
                    entry_price - signal.stop_mult * prior_atr
                    if signal.stop_mult > 0 and np.isfinite(prior_atr)
                    else np.nan
                )
            units = target_units
            if units == 0.0 and had_position:
                finish_position(i, opens[i], "signal", proceeds_booked=True)
        if units > 0 and np.isfinite(stop) and lows[i] <= stop:
            stop_fill = min(opens[i], stop)
            stop_reason = "stop_gap" if opens[i] < stop else "stop_touch"
            record_transaction(i, stop_reason, units * stop_fill, open_equity)
            finish_position(i, stop_fill, "stop")
            blocked = True
        equity[i] = cash + units * closes[i]
        held[i] = units * closes[i] / equity[i] if equity[i] > 0 else 0.0

    if units > 0:  # mark the open trade at the last close without charging an exit that did not happen
        trades.append(
            Trade(
                int(times[entry_index]),
                int(times[-1]),
                entry_price,
                float(closes[-1]),
                equity[-1] / entry_equity - 1.0,
                n - 1 - entry_index,
                "open_at_end",
            )
        )

    equity_series = pd.Series(equity)
    returns = equity_series.pct_change()
    returns.iloc[0] = equity[0] - 1.0
    turnover_by_reason = pd.DataFrame(reason_turnover, columns=TURNOVER_REASONS)
    modeled_costs_by_reason = pd.DataFrame(component_costs, columns=cost_columns)
    if not np.allclose(turnover_by_reason.sum(axis=1).to_numpy(), turnover, rtol=1e-12, atol=1e-12):
        raise RuntimeError("turnover attribution does not reconcile to total turnover")
    if not isclose(float(component_costs.sum()), modeled_cost_paid, rel_tol=1e-10, abs_tol=1e-12):
        raise RuntimeError("modeled cost component attribution does not reconcile to all-in cost")
    return BacktestResult(
        strategy=signal.name,
        equity=equity_series,
        returns=returns,
        open_time=candles["open_time"].reset_index(drop=True),
        in_position=pd.Series(held),
        turnover=pd.Series(turnover),
        trades=trades,
        cost_per_side=cost_per_side,
        modeled_cost_initial_equity=modeled_cost_paid,
        turnover_by_reason=turnover_by_reason,
        modeled_costs_by_component_and_reason=modeled_costs_by_reason,
    )

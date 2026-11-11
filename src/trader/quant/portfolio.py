"""Portfolio bar simulator: shared capital across symbols under the backend risk policy.

What it mirrors from the live path (``risk.validate_intent`` / ``risk.compute_sizing``), per new entry:
- Position size = min(risk budget / loss per unit, notional cap, remaining total-exposure room, free cash), where
  loss per unit = (entry - stop) + entry * cost + stop * cost. ``size_quantity`` is a float mirror of
  ``compute_sizing`` without exchange step rounding; ``tests/unit/test_portfolio.py`` checks parity.
- One position per symbol, at most ``max_open_positions`` at once, stop distance within the policy band,
  daily / 7-day realized-loss limits, the drawdown floor (equity vs running peak) and the cooldown after a loss.
- Positions are never resized after entry (the desk has no add/reduce-to-target flow).

What it shares with ``quant/backtest.py``:
- A signal decided at the close of bar t executes at the OPEN of bar t+1. Traded notional pays ``cost_per_side``.
- Stop fixed at entry: entry - stop_mult * ATR(signal bar), checked against the bar LOW from the entry bar on.
  A bar opening below the stop fills at the open (gap). After a stop-out, re-entry waits for the signal to go flat.

Tie-break (more entry signals than free slots or exposure room): candidates are taken in the fixed ``priority``
order (default: the configured universe order BTC, ETH, SOL, BNB). The rule is data-independent, so it cannot be
tuned on results; its cost is a bias toward earlier symbols in the list, reported via ``rejections``.

Missing bars: a symbol without a candle at a grid time cannot trade or stop out in that bar; its position is
marked at its last known close. Nothing is interpolated into prices.

Not modelled: partial fills, latency, queue position, market impact, exchange step/min-notional rounding.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isclose
from typing import Any

import numpy as np
import pandas as pd

from trader.config import RiskPolicy
from trader.quant.strategies import StrategySignal
from trader.timeutil import bars_per_year, interval_ms

DAY_MS = 86_400_000


@dataclass(frozen=True)
class PortfolioLimits:
    """Risk-policy limits as floats (percent values stay in percent, as in the policy file)."""

    max_risk_per_trade_pct: float
    max_position_notional_pct: float
    max_total_exposure_pct: float
    max_open_positions: int
    min_stop_distance_pct: float
    max_stop_distance_pct: float
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    max_drawdown_kill_pct: float
    cooldown_after_loss_minutes: float

    @classmethod
    def from_policy(cls, policy: RiskPolicy) -> PortfolioLimits:
        return cls(
            max_risk_per_trade_pct=float(policy.max_risk_per_trade_pct),
            max_position_notional_pct=float(policy.max_position_notional_pct),
            max_total_exposure_pct=float(policy.max_total_exposure_pct),
            max_open_positions=int(policy.max_open_positions),
            min_stop_distance_pct=float(policy.min_stop_distance_pct),
            max_stop_distance_pct=float(policy.max_stop_distance_pct),
            daily_loss_limit_pct=float(policy.daily_loss_limit_pct),
            weekly_loss_limit_pct=float(policy.weekly_loss_limit_pct),
            max_drawdown_kill_pct=float(policy.max_drawdown_kill_pct),
            cooldown_after_loss_minutes=float(policy.cooldown_after_loss_minutes),
        )


def size_quantity(
    *,
    equity: float,
    free_quote: float,
    current_exposure: float,
    entry: float,
    stop: float,
    max_risk_pct: float,
    max_position_notional_pct: float,
    max_total_exposure_pct: float,
    cost_rate: float,
    size_scale: float = 1.0,
) -> tuple[float, str]:
    """Float mirror of ``risk.compute_sizing`` (before exchange rounding). Returns (quantity, binding constraint)."""
    if entry <= 0 or stop <= 0 or stop >= entry:
        raise ValueError("stop must be positive and below the entry price for a long position")
    if equity <= 0:
        raise ValueError("equity must be positive")
    risk_budget = equity * max_risk_pct / 100.0 * size_scale
    loss_per_unit = (entry - stop) + entry * cost_rate + stop * cost_rate
    candidates = {
        "risk_budget": risk_budget / loss_per_unit,
        "max_position_notional": equity * max_position_notional_pct / 100.0 / entry,
        "max_total_exposure": max(0.0, equity * max_total_exposure_pct / 100.0 - current_exposure) / entry,
        "free_balance": free_quote / (entry * (1.0 + cost_rate)),
    }
    binding = min(candidates, key=lambda k: candidates[k])
    return max(0.0, candidates[binding]), binding


@dataclass
class PortfolioTrade:
    symbol: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    quantity: float
    stop_price: float
    net_pnl: float
    net_return: float  # net pnl / (entry notional + entry cost)
    bars_held: int
    exit_reason: str  # signal | stop | stop_gap | open_at_end
    binding_constraint: str


@dataclass
class PortfolioResult:
    open_time: np.ndarray  # evaluation grid (bar open times, ms)
    close_time: np.ndarray
    equity: np.ndarray  # at each bar close, in units of initial equity
    returns: pd.Series
    gross_exposure: pd.Series  # sum of position notionals / equity at each bar close
    open_positions: pd.Series
    turnover: pd.Series  # traded notional / equity at the bar open
    trades: list[PortfolioTrade]
    cost_per_side: float
    modeled_cost_by_component: dict[str, float]  # cumulative, as a fraction of initial equity
    rejections: Counter[str] = field(default_factory=Counter)
    interval: str = "1h"
    priority: list[str] = field(default_factory=list)
    symbol_exposure: pd.DataFrame = field(default_factory=pd.DataFrame)  # position value / equity at bar close
    asset_returns: pd.DataFrame = field(default_factory=pd.DataFrame)  # close-to-close, 0 on a missing bar
    bar_cost: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))  # cost / equity at bar open


def _aligned(frame: pd.DataFrame, column: str | pd.Series, grid: np.ndarray) -> np.ndarray:
    values = frame[column] if isinstance(column, str) else column
    series = pd.Series(np.asarray(values, dtype=float), index=frame["open_time"].to_numpy(dtype=np.int64))
    series = series[~series.index.duplicated(keep="first")]
    return series.reindex(grid).to_numpy(dtype=float)


def run_portfolio_backtest(
    candles: dict[str, pd.DataFrame],
    signals: dict[str, StrategySignal],
    *,
    interval: str,
    limits: PortfolioLimits,
    cost_components: dict[str, float],
    start_time_ms: int,
    end_time_ms: int | None = None,
    priority: list[str] | None = None,
    clip_stop_to_policy: bool = False,
) -> PortfolioResult:
    """Simulate long/flat signals for several symbols sharing one account.

    ``candles`` / ``signals`` cover warm-up history too; trading starts at the first grid bar with
    open_time >= ``start_time_ms`` and ends at the last bar with open_time < ``end_time_ms`` (if given).
    ``clip_stop_to_policy``: if the ATR stop is further than the policy's maximum stop distance, place it at that
    maximum instead of skipping the entry (a fixed rule a strategy may declare in advance).
    """
    symbols = list(priority or candles)
    if set(symbols) != set(candles) or set(candles) != set(signals):
        raise ValueError("candles, signals and priority must name the same symbols")
    if not cost_components or any(not np.isfinite(v) or v < 0 for v in cost_components.values()):
        raise ValueError("cost components must be finite and non-negative")
    cost = float(sum(cost_components.values()))
    step = interval_ms(interval)

    grid = np.unique(np.concatenate([c["open_time"].to_numpy(dtype=np.int64) for c in candles.values()]))
    if end_time_ms is not None:
        grid = grid[grid < end_time_ms]
    start_index = int(np.searchsorted(grid, start_time_ms, side="left"))
    if start_index < 1 or start_index >= len(grid):
        raise ValueError("need at least one warm-up bar before start_time_ms and one bar after it")

    k = len(symbols)
    n = len(grid)
    opens = np.vstack([_aligned(candles[s], "open", grid) for s in symbols])
    lows = np.vstack([_aligned(candles[s], "low", grid) for s in symbols])
    closes = np.vstack([_aligned(candles[s], "close", grid) for s in symbols])
    # Decisions: the latest known closed-bar target/ATR (forward-filled across a symbol's missing bars only).
    targets = np.vstack(
        [
            pd.Series(_aligned(candles[s], signals[s].target, grid)).ffill().fillna(0.0).to_numpy()
            for s in symbols
        ]
    )
    atrs = np.vstack(
        [pd.Series(_aligned(candles[s], signals[s].atr, grid)).ffill().to_numpy() for s in symbols]
    )
    stop_mults = [float(signals[s].stop_mult) for s in symbols]
    if np.any((targets < 0) | (targets > 1)):
        raise ValueError("targets must be in [0, 1]")

    last_close = np.full(k, np.nan)
    for j in range(k):  # marks carried in from warm-up
        valid = np.where(np.isfinite(closes[j, :start_index]))[0]
        if len(valid):
            last_close[j] = closes[j, valid[-1]]

    m = n - start_index
    equity_out = np.ones(m)
    exposure_out = np.zeros(m)
    positions_out = np.zeros(m)
    turnover_out = np.zeros(m)
    symbol_exposure_out = np.zeros((m, k))  # position value / equity at each bar close
    asset_return_out = np.zeros((m, k))  # close-to-close return vs the last known close (0 on a missing bar)
    bar_cost_out = np.zeros(m)  # modeled cost paid in the bar / equity at the bar open
    cost_paid = dict.fromkeys(cost_components, 0.0)
    rejections: Counter[str] = Counter()
    trades: list[PortfolioTrade] = []

    cash = 1.0
    units = np.zeros(k)
    stop = np.full(k, np.nan)
    entry_price = np.zeros(k)
    entry_cost_basis = np.zeros(k)
    entry_index = np.full(k, -1)
    entry_binding = [""] * k
    blocked = np.zeros(k, dtype=bool)
    peak = 1.0
    realized: list[tuple[int, float]] = []  # (exit bar open time, pnl)
    last_loss_time: int | None = None

    traded_notional = [0.0]  # cumulative, for the cost reconciliation check

    def pay(notional: float) -> float:
        traded_notional[0] += notional
        for name, rate in cost_components.items():
            cost_paid[name] += notional * rate
        return notional * cost

    def close_position(j: int, i: int, price: float, reason: str) -> None:
        nonlocal cash, last_loss_time
        notional = units[j] * price
        proceeds = notional - pay(notional)
        pnl = proceeds - entry_cost_basis[j]
        cash += proceeds
        trades.append(
            PortfolioTrade(
                symbols[j], int(grid[entry_index[j]]), int(grid[i]), float(entry_price[j]), float(price),
                float(units[j]), float(stop[j]), float(pnl), float(pnl / entry_cost_basis[j]),
                int(i - entry_index[j]), reason, entry_binding[j],
            )
        )  # fmt: skip
        realized.append((int(grid[i]), float(pnl)))
        if pnl < 0:
            last_loss_time = int(grid[i])
        units[j] = 0.0
        stop[j] = np.nan

    for i in range(start_index, n):
        t = int(grid[i])
        row = i - start_index
        has_bar = np.isfinite(opens[:, i])
        mark_open = np.where(has_bar, opens[:, i], last_close)
        open_equity = cash + float(np.nansum(units * mark_open))
        traded = 0.0
        cost_before_bar = sum(cost_paid.values())

        # 1. exits on a flat signal (and reset the post-stop block)
        for j in range(k):
            if targets[j, i - 1] <= 0.0:
                blocked[j] = False
            if units[j] > 0 and has_bar[j] and targets[j, i - 1] <= 0.0:
                traded += units[j] * opens[j, i]
                close_position(j, i, float(opens[j, i]), "signal")

        # 2. entries in priority order, each checked against the account state after earlier fills this bar
        for j in range(k):
            if units[j] > 0 or blocked[j] or targets[j, i - 1] <= 0.0:
                continue
            if not has_bar[j] or not np.isfinite(atrs[j, i - 1]) or stop_mults[j] <= 0:
                rejections["no_bar_or_no_atr"] += 1
                continue
            equity_now = cash + float(np.nansum(units * mark_open))
            exposure_now = float(np.nansum(units * mark_open))
            if int((units > 0).sum()) >= limits.max_open_positions:
                rejections["max_open_positions"] += 1
                continue
            day_start = t - t % DAY_MS
            week_start = t - 7 * DAY_MS
            if (
                sum(p for when, p in realized if when >= day_start)
                <= -equity_now * limits.daily_loss_limit_pct / 100
            ):
                rejections["daily_loss_limit"] += 1
                continue
            if (
                sum(p for when, p in realized if when >= week_start)
                <= -equity_now * limits.weekly_loss_limit_pct / 100
            ):
                rejections["weekly_loss_limit"] += 1
                continue
            if equity_now <= peak * (1 - limits.max_drawdown_kill_pct / 100):
                rejections["drawdown_floor"] += 1
                continue
            if (
                last_loss_time is not None
                and t < last_loss_time + limits.cooldown_after_loss_minutes * 60_000
            ):
                rejections["cooldown_after_loss"] += 1
                continue
            entry = float(opens[j, i])
            stop_price = entry - stop_mults[j] * float(atrs[j, i - 1])
            if clip_stop_to_policy:
                stop_price = max(stop_price, entry * (1 - limits.max_stop_distance_pct / 100))
            distance_pct = (entry - stop_price) / entry * 100
            if not limits.min_stop_distance_pct <= distance_pct <= limits.max_stop_distance_pct:
                rejections["stop_distance_outside_policy"] += 1
                continue
            quantity, binding = size_quantity(
                equity=equity_now,
                free_quote=cash,
                current_exposure=exposure_now,
                entry=entry,
                stop=stop_price,
                max_risk_pct=limits.max_risk_per_trade_pct,
                max_position_notional_pct=limits.max_position_notional_pct,
                max_total_exposure_pct=limits.max_total_exposure_pct,
                cost_rate=cost,
            )
            if quantity * entry <= 1e-9 * equity_now:
                rejections[f"zero_size_{binding}"] += 1
                continue
            notional = quantity * entry
            basis = notional + pay(notional)
            cash -= basis
            traded += notional
            units[j] = quantity
            stop[j] = stop_price
            entry_price[j] = entry
            entry_cost_basis[j] = basis
            entry_index[j] = i
            entry_binding[j] = binding

        # 3. protective stops against the bar low (entry bar included)
        for j in range(k):
            if units[j] > 0 and has_bar[j] and np.isfinite(stop[j]) and lows[j, i] <= stop[j]:
                gap = opens[j, i] < stop[j]
                fill = float(opens[j, i]) if gap else float(stop[j])
                traded += units[j] * fill
                close_position(j, i, fill, "stop_gap" if gap else "stop")
                blocked[j] = True

        # 4. mark to market at the close
        has_close = np.isfinite(closes[:, i]) & np.isfinite(last_close) & (last_close > 0)
        asset_return_out[row] = np.where(has_close, closes[:, i] / np.where(has_close, last_close, 1.0) - 1.0, 0.0)
        last_close = np.where(np.isfinite(closes[:, i]), closes[:, i], last_close)
        values = np.nan_to_num(units * last_close)
        held_value = float(values.sum())
        equity_out[row] = cash + held_value
        peak = max(peak, equity_out[row])
        exposure_out[row] = held_value / equity_out[row] if equity_out[row] > 0 else 0.0
        symbol_exposure_out[row] = values / equity_out[row] if equity_out[row] > 0 else 0.0
        positions_out[row] = float((units > 0).sum())
        turnover_out[row] = traded / open_equity if open_equity > 0 else 0.0
        bar_cost_out[row] = (sum(cost_paid.values()) - cost_before_bar) / open_equity if open_equity > 0 else 0.0

    for j in range(k):  # mark open trades at the end without charging an exit that did not happen
        if units[j] > 0:
            value = units[j] * last_close[j]
            pnl = value - entry_cost_basis[j]
            trades.append(
                PortfolioTrade(
                    symbols[j], int(grid[entry_index[j]]), int(grid[-1]), float(entry_price[j]),
                    float(last_close[j]), float(units[j]), float(stop[j]), float(pnl),
                    float(pnl / entry_cost_basis[j]), int(n - 1 - entry_index[j]), "open_at_end", entry_binding[j],
                )
            )  # fmt: skip

    if not isclose(sum(cost_paid.values()), traded_notional[0] * cost, rel_tol=1e-9, abs_tol=1e-15):
        raise RuntimeError("cost components do not reconcile to traded notional x cost per side")
    returns = pd.Series(equity_out).pct_change()
    returns.iloc[0] = equity_out[0] - 1.0
    eval_grid = grid[start_index:]
    return PortfolioResult(
        open_time=eval_grid,
        close_time=eval_grid + step - 1,
        equity=equity_out,
        returns=returns,
        gross_exposure=pd.Series(exposure_out),
        open_positions=pd.Series(positions_out),
        turnover=pd.Series(turnover_out),
        trades=trades,
        cost_per_side=cost,
        modeled_cost_by_component=cost_paid,
        rejections=rejections,
        interval=interval,
        priority=symbols,
        symbol_exposure=pd.DataFrame(symbol_exposure_out, columns=symbols),
        asset_returns=pd.DataFrame(asset_return_out, columns=symbols),
        bar_cost=pd.Series(bar_cost_out),
    )


def daily_returns(result: PortfolioResult) -> pd.Series:
    """UTC-day returns from the bar-close equity curve (first day measured from the initial equity of 1.0)."""
    days = pd.to_datetime(result.close_time, unit="ms", utc=True).floor("D")
    last_by_day = pd.Series(result.equity, index=days).groupby(level=0).last()
    previous = last_by_day.shift(1)
    previous.iloc[0] = 1.0
    return last_by_day / previous - 1.0


def portfolio_summary(result: PortfolioResult) -> dict[str, Any]:
    """Portfolio-level return, drawdown, turnover, costs and exposure (bootstrap CIs live in ``stats``)."""
    from trader.quant.stats import performance_metrics

    ppy = bars_per_year(result.interval)
    metrics = performance_metrics(result.returns, result.gross_exposure, result.trades, ppy, result.turnover)
    closed = [t for t in result.trades if t.exit_reason != "open_at_end"]
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in result.priority:
        mine = [t for t in closed if t.symbol == symbol]
        by_symbol[symbol] = {
            "trades": len(mine),
            "net_pnl_initial_equity": float(sum(t.net_pnl for t in mine)),
            "win_rate": (sum(t.net_pnl > 0 for t in mine) / len(mine)) if mine else None,
        }
    years = len(result.returns) / ppy if ppy else 0.0
    costs_bps = {name: value * 10_000 for name, value in result.modeled_cost_by_component.items()}
    costs_bps["total"] = float(sum(costs_bps.values()))
    start = datetime.fromtimestamp(int(result.open_time[0]) / 1000, tz=UTC)
    end = datetime.fromtimestamp(int(result.close_time[-1]) / 1000, tz=UTC) + timedelta(milliseconds=1)
    return {
        **metrics,
        "evaluation_start": start.isoformat().replace("+00:00", "Z"),
        "evaluation_end_exclusive": end.isoformat().replace("+00:00", "Z"),
        "mean_gross_exposure": float(result.gross_exposure.mean()),
        "max_gross_exposure": float(result.gross_exposure.max()),
        "mean_open_positions": float(result.open_positions.mean()),
        "share_of_time_invested": float((result.open_positions > 0).mean()),
        "modeled_cost_bps_initial_equity": costs_bps,
        "modeled_cost_bps_initial_equity_per_year": (costs_bps["total"] / years) if years > 0 else None,
        "trades_by_symbol": by_symbol,
        "trades_open_at_end": len(result.trades) - len(closed),
        "entry_rejections": dict(sorted(result.rejections.items())),
        "binding_constraints": dict(Counter(t.binding_constraint for t in closed)),
        "exit_reasons": dict(Counter(t.exit_reason for t in closed)),
        "tie_break": "fixed priority order " + " > ".join(result.priority),
    }

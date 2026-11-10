# Trading risk policy — draft

The enforceable policy is [`configs/risk_policy.yaml`](../configs/risk_policy.yaml). This note explains each control.
**Every number is provisional**: chosen to be conservative, not optimised, and not backed by evidence about this
system's behaviour. The file carries `status: provisional`; LIVE mode refuses to run until an operator has reviewed
the values and set `status: approved`.

| Control | Draft value | Reasoning |
|---|---|---|
| Allowed instruments | spot | ADR 0002 |
| Allowed symbols | BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT | Research universe |
| Opening side | BUY only; SELL only reduces an existing position | Spot, no margin |
| Order types | MARKET, LIMIT (marketable) | Simplest fill semantics |
| Max leverage | 1 | No leverage |
| Max risk per trade | 0.5% of equity | Small enough that 20 consecutive full losses cost ≈10% |
| Max position notional | 20% of equity | Caps gap risk beyond the stop |
| Max total exposure | 50% of equity | The four symbols are highly correlated; treat as one bet |
| Max open positions | 2 | Same reason |
| Daily / weekly realized loss limit | 2% / 5% of equity | Stops trading before the drawdown limit is near |
| Drawdown kill threshold | 10% from equity peak | Engages `stop_new_orders` |
| Stop-loss | Required; 0.3%–10% from entry | A stop tighter than typical spread+noise is a guaranteed fee donation; wider than 10% makes the notional cap the real limit |
| Max spread | 10 bps | Majors are normally ≪ 5 bps; more means stress |
| Max expected slippage | 15 bps | Reject when the paper/demo slippage model exceeds it |
| Min 24h quote volume | 50M USDT | Liquidity floor |
| Max quote age | 15 s | Fail closed on stale prices |
| Max candle staleness | 2 bars of the decision timeframe | Signals must come from current data |
| Cooldown after a losing close | 60 min per symbol | Guards against revenge re-entry; unvalidated |
| Min reward/risk | not enforced | No validated model to justify a number |
| Intent TTL / approval TTL | 30 min / 10 min | Prices move; approvals must be fresh |
| Human approval | PAPER: Claude Code permission prompt; DEMO/LIVE: CLI at a terminal | ADR 0005 |

## Sizing

```
risk_budget   = equity × max_risk_per_trade × size_scale        (size_scale ∈ (0, 1], chosen by the analyst)
loss_per_unit = (entry − stop) + entry×(fee + slippage) + stop×(fee + slippage)
quantity      = floor_to_step( min( risk_budget / loss_per_unit,
                                    max_position_notional / entry,
                                    remaining_exposure / entry,
                                    free_quote_balance / (entry × (1 + fee + slippage)) ) )
```

All Decimal. The analyst may only scale risk **down**. If the result is below `minQty`/`minNotional` the intent is
rejected rather than rounded up.

## Kill switch

Three separate actions with different blast radius:

- `stop_new_orders` — MCP tool or CLI. Blocks intents that open or add. Reducing sells stay possible.
- `cancel_open_orders` — optional flag on the same tool; cancels resting entry orders. Protective stops are **not** cancelled.
- `close_positions` — deliberately not automated. The tool reports positions; closing goes through the normal intent + approval path.

Release: `python -m trader kill-switch release` at a terminal.

## Before LIVE

1. Review every value above against your capital and tolerance; set `status: approved` and bump `version`.
2. Create a live key: spot trading only, no withdrawals, IP-restricted.
3. Complete a Demo Mode period with reconciled fills and measured slippage.
4. `python -m trader live-unlock` (expires; repeat per session).

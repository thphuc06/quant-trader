# ADR 0002 — Spot, long/flat, four majors, hourly decisions

Status: accepted · 2026-11-10

## Decision
- Instrument: Binance **Spot**. Futures data (funding, open interest) is read as context only.
- Universe: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT.
- Decision timeframe 1h on closed bars; 4h and 1d for context.
- Rehearsal environment: Binance **Demo Mode** (not Spot Testnet).

## Why
- No leverage/liquidation/funding accounting to get wrong.
- Round-trip cost (~20 bps fees + spread + slippage, provisional) is small relative to typical multi-hour moves but
  large relative to 5m/15m moves; and human approval adds minutes of latency.
- Years of free hourly data for these symbols; few parameters.
- Demo Mode mirrors live filters and prices; Testnet has an independent order book.

## Consequences
- No short side. `SHORT_CANDIDATE` is analysis-only.
- Results are conditional on a hindsight-selected, highly correlated universe; exposure limits treat it as one bet.
- Intraday and order-book ideas are out of scope until a collector has accumulated history.

Detail: [research/strategy-landscape.md](../../research/strategy-landscape.md).

# Binance API research

All facts are **verified** against official docs on 2026-11-10 unless marked otherwise. Source IDs: [source-register.md](source-register.md).
Base URLs live in `configs/app.yaml`, never in code.

## Environments

| Mode | REST base | Streams | Keys | Notes |
|---|---|---|---|---|
| Market data (all modes) | `https://data-api.binance.vision` | `wss://stream.binance.com:9443` | none | Public, market-data-only host (BN-GEN). Used even in PAPER so prices are real |
| `PAPER` | — (no order endpoint is ever called) | — | none | Local simulator fed by real quotes |
| `BINANCE_DEMO` | `https://demo-api.binance.com` | `wss://demo-stream.binance.com` | created at demo.binance.com | Same filters/limits as live, prices similar to live (BN-DEMO) |
| `LIVE` | `https://api.binance.com` | `wss://stream.binance.com:9443` | production | Locked by default; see ADR 0005 |

**Demo Mode vs Spot Testnet.** Testnet has an independent order book and monthly resets; Demo Mode mirrors live
features and prices. Demo Mode is the better rehearsal for a strategy that depends on realistic prices → chosen.
Caveat from the docs: "realistic market data is not equal to real market data".

There is no implicit fallback between modes: each mode reads its own credential variables
(`BINANCE_DEMO_API_KEY/SECRET`, `BINANCE_LIVE_API_KEY/SECRET`) and its own base URL; a missing credential is an error,
never a reason to try another environment.

## Endpoints used

| Purpose | Endpoint | Weight | Security |
|---|---|---|---|
| Candles | `GET /api/v3/klines` (limit ≤ 1000) | 2 | NONE |
| Symbol filters | `GET /api/v3/exchangeInfo` | 20 (**background** – weight not re-read) | NONE |
| 24h stats | `GET /api/v3/ticker/24hr` | 2 per symbol | NONE |
| Best bid/ask | `GET /api/v3/ticker/bookTicker` | 2 per symbol | NONE |
| Server time | `GET /api/v3/time` | 1 (**background**) | NONE |
| Account | `GET /api/v3/account` | 20 | USER_DATA |
| New order | `POST /api/v3/order` | 1 | TRADE |
| Query order | `GET /api/v3/order` (by `origClientOrderId`) | 4 | USER_DATA |
| Cancel order | `DELETE /api/v3/order` | 1 | TRADE |
| Open orders | `GET /api/v3/openOrders` | 6 per symbol | USER_DATA |
| Funding / mark | `GET https://fapi.binance.com/fapi/v1/premiumIndex` | 1 | NONE |
| Open interest | `GET https://fapi.binance.com/fapi/v1/openInterest` | 1 | NONE |

Kline array layout: `[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]`.
Prices and quantities are strings; we parse them to `Decimal` for anything that touches an order, `float64` only for research features.

Deliberately **not** implemented: withdraw, transfer, margin, sub-account, leverage, and any generic "send signed request" function.

## Signing and time

- HMAC-SHA256 over the query string, hex digest appended as `signature`; key in `X-MBX-APIKEY`.
- `timestamp` in ms; `recvWindow` 5000 (doc default; max 60000). The client measures server-time offset at start and refuses to sign if local drift exceeds 1 s after correction.
- Docs recommend Ed25519 keys. HMAC is implemented first because Demo keys are HMAC by default; Ed25519 is a listed follow-up.
- 2026-11-11: Ed25519 implemented (`Ed25519Signer`): the same query string is signed with the private key, the
  signature is base64-encoded and sent percent-encoded (Spot request-security docs; Binance.US docs state the
  percent-encoding explicitly for REST). Verified only with locally generated keys against mocked HTTP. **Not yet
  verified:** whether the Demo portal offers the "self-generated" key type, and a real signed call.

## Rate limits and failure handling

- Limits are per IP. The client tracks `X-MBX-USED-WEIGHT-1M` and slows down before the limit.
- `429` → stop, honour `Retry-After`. `418` → banned; the client raises and does not retry. Repeating after 429 escalates bans from 2 minutes to 3 days.
- Idempotent `GET`s are retried with exponential backoff and jitter on timeouts and 5XX.
- **Orders are never blindly retried.** A timeout or 5XX on `POST /api/v3/order` means the result is unknown (BN-GEN).
  The execution service then queries `GET /api/v3/order` by the deterministic `newClientOrderId`; only a definite
  "order does not exist" answer allows a new attempt, and only with the same client order ID.

## Order filters

Applied in `src/trader/exchanges/binance/filters.py` before any order leaves the process:

- `PRICE_FILTER`: price within `[minPrice, maxPrice]` and a multiple of `tickSize`.
- `LOT_SIZE` / `MARKET_LOT_SIZE`: quantity within bounds and a multiple of `stepSize` (we always round **down**).
- `MIN_NOTIONAL` / `NOTIONAL`: `price × quantity` within bounds (for market orders, checked against the current ask/bid).

Filters are cached with a timestamp and refreshed when older than the policy's `max_data_age`. A precision change on the exchange therefore results in a refresh, not a rejected order loop.

## WebSocket

- Raw stream: `wss://stream.binance.com:9443/ws/<symbol>@kline_<interval>`; combined: `/stream?streams=a/b`.
- A kline is final only when `k.x` is `true`; the collector stores closed klines only.
- **background** (not re-read this session): connections last at most 24 h and the server pings periodically. The
  collector reconnects with capped exponential backoff on any close and back-fills the gap over REST.
- The MVP does **not** depend on the collector. Interactive sessions refresh candles over REST on demand; the
  collector exists for users who want warm data. User-data streams are deferred — fills are reconciled by REST polling.

## Security checklist for API keys

- Never enable withdrawal permission. Enable only "reading" and "spot trading".
- Restrict the key to a fixed IP.
- Separate keys for Demo and Live; never reuse.
- Keys live in `.env` (git-ignored) or the OS environment, are held as `SecretStr`, and are never logged or returned by a tool.
- Rotation: delete the key in Binance API Management, create a new one, update `.env`, restart the MCP server.

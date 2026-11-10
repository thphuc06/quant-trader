---
id: kb-mechanics-binance-spot
title: Binance Spot — costs, filters, order states and what can go wrong
type: market_mechanics
status: candidate
tags: [binance, spot, fees, filters, execution]
created: 2026-11-10
updated: 2026-11-10
author: Claude (Fable 5.2), builder session
confidence: medium
applicability: Binance Spot, all universe symbols
sources:
  - {title: "Binance Spot REST – general info", url: "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information", accessed: 2026-11-10}
  - {title: "Binance Spot – filters", url: "https://developers.binance.com/docs/binance-spot-api-docs/filters", accessed: 2026-11-10}
  - {title: "Binance Spot – Demo Mode", url: "https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info", accessed: 2026-11-10}
limitations: The fee figure comes from a secondary source; the account's real rate must be read from the account endpoint. Endpoint weights change.
related: []
---

## Costs of a round trip

| Component | Working assumption | Status |
|---|---|---|
| Fee | 10 bps per side for a regular account | provisional (secondary source) |
| Spread | about 1 bp on the majors in normal conditions; read the live value from `get_market_snapshot` | measured live |
| Slippage | 2 bps per side | assumption until paper/demo fills say otherwise |

A long trade therefore starts roughly 25 bps behind. A setup whose plausible move is not several times that is not
worth taking. Fees may be charged in the asset received (the base asset on a buy), which slightly reduces the quantity
actually held; the execution service accounts for this.

## Filters the backend applies

- Price must be a multiple of `tickSize`; quantity a multiple of `stepSize` (always rounded **down**).
- `price × quantity` must be at least `minNotional`. A risk-sized position smaller than that is rejected, not rounded up.
- Filters can change; they are refreshed from `exchangeInfo`, never hard-coded.

## Order outcomes

- `FILLED`, `PARTIALLY_FILLED`, `NEW`, `CANCELED`, `REJECTED`, `EXPIRED` are definite.
- A timeout or HTTP 5xx on order placement is **not** a rejection: the order may exist. The backend marks the intent
  `UNKNOWN`, queries by client order ID, and blocks the symbol until resolved. Never ask for the order to be sent again.
- HTTP 429 means stop; continuing leads to an IP ban (418).

## Environments

- `PAPER`: simulated fills at the live best quote plus configured slippage. No partial fills. Stops are settled against
  historical bars when positions are next reviewed — nothing watches a paper position between sessions.
- `BINANCE_DEMO`: Binance Demo Mode. Same filters and limits as live, prices similar to live, virtual funds.
  The exchange holds the stop order. Demo fills are *realistic*, not *real*.
- `LIVE`: locked by default; see `docs/risk-and-execution.md`.

## Spot means long/flat

Without margin there is no short side. A bearish view can only be expressed as "do not hold" or "reduce".

# ADR 0001 — Modular monolith, SQLite + Parquet, stdio MCP

Status: accepted · 2026-11-10

## Context
One operator, one machine, interactive Claude Code sessions. The master prompt proposed PostgreSQL, DuckDB,
docker-compose and separate apps (api, collector, orchestrator, scheduler).

## Decision
- A single installable package `src/trader` with a `TradingDesk` facade.
- SQLite (WAL) for operational data; Parquet (pyarrow) for candles; plain files for models and knowledge.
- A stdio MCP server started by Claude Code. No HTTP API, no scheduler, no message bus.
- Synchronous domain code; the optional WebSocket collector is a separate process.

## Strongest alternative considered
Service-oriented: Postgres + a 24/7 collector + a FastAPI risk/execution service, with the MCP server as a thin
client over HTTP. **Better at**: a hard process/credential boundary between LLM and execution (the service could run
as another user and hold the keys), continuous data, concurrent writers, monitoring stops without a session.
**Worse at**: operational burden for one person, many more failure modes to test (service down, auth between
processes, migrations), and it solves problems this MVP does not have yet (it trades hourly bars with human approval).

## Consequences
- The LLM/execution boundary is logical, not physical (see threat model T3). Accepted for PAPER/DEMO; revisit before LIVE.
- No always-on stop monitoring in PAPER; stops are replayed against 1m bars when a session opens.
- Moving to the service design later means putting `TradingDesk` behind HTTP; module boundaries were drawn for that.

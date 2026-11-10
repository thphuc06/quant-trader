# Research log

All entries 2026-11-10 (UTC+7 working day). Tools: Claude Code built-in WebSearch/WebFetch, direct HTTP calls for API probes.

| # | Action | Outcome |
|---|---|---|
| 1 | Inspected the directory | Only the master prompt; no git repo; Python 3.13, git, Docker present; `uv` absent → plain `venv` + `pip` |
| 2 | `pip index versions mcp` | Latest is **2.3.0**, a major version beyond what the prompt's examples assume |
| 3 | Read Binance general REST info | Found market-data-only host `data-api.binance.vision`; 429/418 semantics; 5XX = unknown execution state |
| 4 | Looked for Spot Testnet page | The page had no URLs; search led to **Demo Mode**, a separate environment with live-like prices → chosen over Testnet |
| 5 | Read Claude Code hooks, skills, permissions, MCP, subagents, headless docs | Confirmed formats; learned a repo cannot self-approve its MCP server and that `-p` skips trust prompts |
| 6 | Read MCP SDK front page, migration guide, testing page | `FastMCP` → `MCPServer`; in-memory `Client` for tests |
| 7 | Read Binance market-data, trading, account, filters, request-security pages | Endpoint table in `binance-api-research.md` |
| 8 | Searched DSR, PBO, crypto momentum papers | Citations confirmed; full texts not re-read (marked `literature`/`background`) |
| 9 | Compared search APIs; read Tavily and Brave references | Pricing claims conflict across secondary sources → no dependence on free tiers |
| 10 | Searched Binance fees | Official fee page not retrieved; 10 bps taken as a provisional assumption |
| 11 | Probed `data-api.binance.vision` klines and `fapi.binance.com` premiumIndex/openInterest | All HTTP 200 from this machine |
| 12 | Read WebSocket streams page | Base URLs and kline payload confirmed; ping/lifetime section truncated |

## What changed relative to the master prompt

| Prompt suggestion | Decision | Why |
|---|---|---|
| PostgreSQL + Parquet/DuckDB, docker-compose | **SQLite (WAL) + Parquet**, no containers | One user, one machine, one writer. Postgres adds an always-on service and a failure mode ("database unavailable") without a matching need. The repository layer is small enough to port later |
| `apps/`, `core/`, `exchanges/`, `mcp_server/` top-level trees | One package `src/trader/` | A single installable package keeps imports and tests simple |
| ~45 MCP tools | ~25 task-shaped tools | Fewer, larger tools are easier for the model to choose correctly and keep context small |
| 9 skills, 4 subagents | 4 skills, 2 subagents | Each removed item duplicated another's job |
| uv + `uv.lock` | `pip` + pinned `requirements.lock` | `uv` is not installed here; not worth forcing |
| News subsystem with dedup/entity resolution/event classification | Provider abstraction + point-in-time archive + safe fetch | No archive exists to validate anything richer |
| ML "prediction service" | Offline pipeline + model card; predictions exposed but flagged unvalidated | Nothing should look more trustworthy than it is |
| Freqtrade/VectorBT/Backtrader | Custom simulator | See `ml-and-validation.md` §5 |
| One-time approval token passed through the model | Approval recorded by a **human-only CLI** (needs a TTY); the server consumes it | A token that travels through the LLM context can be replayed or leaked by the LLM |
| Futures adapter, user-data stream, macro calendar, on-chain | Deferred | Listed under deferred scope in `architecture/system-design.md` |

## Open questions carried forward

- Real fee tier of the operator's account (read from `GET /api/v3/account` once keys exist).
- Measured slippage in Demo Mode vs the 2 bps assumption.
- Whether Demo Mode klines are reliable enough to drive signals (we use production market data instead).
- An official, stable feed for exchange announcements and a licensed macro calendar.

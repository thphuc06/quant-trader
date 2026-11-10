# Source register

Access date for every row: **2026-11-10**. Confidence is about the *claim as we use it*, not the source in general.

## Official documentation (verified this session)

| ID | Source | URL | Claim we rely on | Conf. | Limits |
|---|---|---|---|---|---|
| BN-GEN | Binance Spot REST – general info | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information | Base URLs (`api.binance.com`, `api1-4`, `api-gcp`, market-data-only `data-api.binance.vision`); ms timestamps; 429 = back off, 418 = auto-ban (2 min → 3 days); `X-MBX-USED-WEIGHT-*` and `Retry-After` headers; limits are per IP; 5XX means execution status **unknown** | high | Read through a summarising fetch tool, not raw HTML |
| BN-MD | Binance Spot – market data endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints | `/api/v3/klines` weight 2, limit ≤ 1000, 12-field array layout, interval list; `/ticker/24hr`, `/ticker/bookTicker`, `/depth` weights and params | high | Weights change over time; re-check on upgrade |
| BN-SEC | Binance Spot – request security | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security | SIGNED = HMAC-SHA256 over query string + body; `X-MBX-APIKEY`; `recvWindow` default 5000, max 60000; Ed25519 keys recommended | high | We implement HMAC only (Ed25519 deferred) |
| BN-TRADE | Binance Spot – trading endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints | `POST /api/v3/order` params and per-type mandatory fields; `newClientOrderId`; cancel by `origClientOrderId` | high | FULL response schema not visible in the fetch; fields we parse are covered by contract tests on documented names only |
| BN-ACCT | Binance Spot – account endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints | `GET /api/v3/order` can query by `origClientOrderId` (weight 4); `GET /api/v3/account` weight 20 and fields; `myTrades` | high | Order `status` enum not listed on that page |
| BN-FILT | Binance Spot – filters | https://developers.binance.com/docs/binance-spot-api-docs/filters | PRICE_FILTER / LOT_SIZE / MARKET_LOT_SIZE / MIN_NOTIONAL / NOTIONAL rules | high | — |
| BN-DEMO | Binance Spot – Demo Mode | https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info | Demo REST `https://demo-api.binance.com/api`, WS `wss://demo-stream.binance.com`; keys created at demo.binance.com; filters/limits "exactly the same" as live; prices similar to live; "realistic market data is not equal to real market data" | high | Not exercised with real demo keys in this build |
| BN-WS | Binance Spot – WebSocket streams | https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams | `wss://stream.binance.com:9443` (and `:443`); raw `/ws/<stream>` and combined `/stream?streams=`; `<symbol>@kline_<interval>`; `k.x` = kline closed | high | Ping/pong and 24h-lifetime text was truncated in the fetch → treated as **background** |
| BN-FUT | Binance USDⓈ-M – mark price | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price | `GET https://fapi.binance.com/fapi/v1/premiumIndex` (funding, mark, index); `GET /fapi/v1/openInterest` | high | Also observed live: both returned HTTP 200 from this machine |
| CC-HOOK | Claude Code – hooks | https://code.claude.com/docs/en/hooks | Event list (SessionStart, PreToolUse, PostToolUse, PreCompact, Stop, …); settings format; exit 2 or `permissionDecision: deny` blocks; `additionalContext`; MCP matcher `mcp__server__.*` | high | Page is very long; only the first ~100k chars were read |
| CC-SKILL | Claude Code – skills | https://code.claude.com/docs/en/skills | `.claude/skills/<name>/SKILL.md`; only descriptions are always in context, body loads on invocation; frontmatter fields; `$ARGUMENTS` | high | — |
| CC-PERM | Claude Code – permissions | https://code.claude.com/docs/en/permissions | allow/ask/deny; deny wins; `Read(.env)`; MCP rule forms; a hook `allow` does not override deny/ask rules | high | — |
| CC-MCP | Claude Code – MCP | https://code.claude.com/docs/en/mcp | `.mcp.json` stdio entry; `${VAR:-default}` expansion; project servers need user approval; a repo cannot self-approve its servers; 25k-token default MCP output limit | high | — |
| CC-AGENT | Claude Code – subagents | https://code.claude.com/docs/en/sub-agents | `.claude/agents/*.md`; `tools` allowlist; `mcp__server__*`; omit `Agent` to stop nesting | high | — |
| CC-HEADLESS | Claude Code – run programmatically | https://code.claude.com/docs/en/headless | `claude -p`; `--bare` "doesn't use your subscription login", needs `ANTHROPIC_API_KEY`; `-p` runs project hooks and MCP servers without trust prompts | high | Does not by itself state a policy on subscription use in third-party products |
| MCP-PY | MCP Python SDK (PyPI) | https://pypi.org/project/mcp/ | Latest 2.3.0 (2026-10-02); `from mcp.server import MCPServer`; Python ≥ 3.10 | high | — |
| MCP-MIG | MCP Python SDK – migration guide | https://py.sdk.modelcontextprotocol.io/migration/ | `FastMCP` → `MCPServer`; decorators unchanged; `run()` per-transport; `ToolError` in `mcp.server.mcpserver.exceptions`; snake_case result fields | high | Only first part of the page was read |
| MCP-TEST | MCP Python SDK – testing | https://py.sdk.modelcontextprotocol.io/get-started/testing/ | In-memory `Client(server, raise_exceptions=True)` | high | — |
| TAV | Tavily search API reference | https://docs.tavily.com/documentation/api-reference/endpoint/search | `POST https://api.tavily.com/search`, bearer auth, `topic: news`, `time_range`, `published_date` for news | high | — |
| BRV | Brave Search API – get started | https://api-dashboard.search.brave.com/app/documentation/web-search/get-started | `GET https://api.search.brave.com/res/v1/web/search`, `X-Subscription-Token`, `freshness` | high | Result date fields not confirmed |

## Literature

| ID | Source | URL | Claim | Conf. | Limits |
|---|---|---|---|---|---|
| DSR | Bailey & López de Prado (2014), "The Deflated Sharpe Ratio", J. Portfolio Management 40(5) | https://papers.ssrn.com/abstract=2460551 | Sharpe must be deflated for the number of trials and for non-normal returns; DSR = PSR evaluated against the expected maximum Sharpe of N trials | high | Formula implemented from background knowledge; abstract checked, PDF not re-read |
| PBO | Bailey, Borwein, López de Prado, Zhu (2017), "The Probability of Backtest Overfitting", J. Computational Finance 20(4), DOI 10.21314/jcf.2016.322 | https://papers.ssrn.com/abstract=2326253 | CSCV estimates the probability that the in-sample best configuration underperforms the median out of sample | high | Same as above |
| LT | Liu & Tsyvinski (2021), "Risks and Returns of Cryptocurrency", RFS 34(6) (NBER w24877) | https://www.nber.org/papers/w24877 | Time-series momentum at daily/weekly horizons in BTC/ETH/XRP; crypto returns load little on traditional factors | medium | Sample ends 2018; no transaction costs at our frequency; may have decayed; says nothing about 1h bars |
| AFML | López de Prado (2018), *Advances in Financial Machine Learning* | (book) | Triple-barrier labels, meta-labelling, purged K-fold with embargo, sample uniqueness | medium | **background** – not re-fetched; used as method description, not as evidence of profitability |

## Secondary / low-confidence (leads only)

| ID | Source | URL | Used for | Conf. |
|---|---|---|---|---|
| FEE-SQ | Binance Square post summarised by search | https://www.binance.com/square/post/25992597333673 | Regular spot fee 0.10% maker / 0.10% taker, 25% BNB discount | low–medium (official fee page not fetched; the account's real rate is read from `GET /api/v3/account` `commissionRates` when keys exist) |
| SRCH-CMP | Assorted 2026 comparisons of search APIs | https://www.beri.net/article/tavily-vs-exa-vs-brave-vs-perplexity-search-api-agent-cost-2026 | Free-tier figures for Tavily/Brave/Exa disagree across sources | low — we do not depend on a free tier |

## Observed directly (this machine, 2026-11-10)

- `GET https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2` → HTTP 200, header `x-mbx-used-weight-1m: 2`, 12-field arrays as documented.
- `GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT` and `/fapi/v1/openInterest` → HTTP 200.
- `pip index versions mcp` → 2.3.0 is the newest release.

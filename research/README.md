# Research (Phase 0)

Research done **before** implementation, on 2026-11-10. Everything here is a summary in our own words with links; no article text is copied.

| File | Question it answers |
|---|---|
| [research-log.md](research-log.md) | What was looked up, in what order, what changed our mind |
| [source-register.md](source-register.md) | Every source, with access date, supported claim, confidence, limits |
| [strategy-landscape.md](strategy-landscape.md) | Market/instrument scope, strategy families, features, recommendation matrix |
| [ml-and-validation.md](ml-and-validation.md) | ML methods, labels, backtest validity, metrics |
| [news-intelligence-research.md](news-intelligence-research.md) | Web/news subsystem, source reliability, prompt injection |
| [binance-api-research.md](binance-api-research.md) | Verified Binance endpoints, environments, limits, order safety |
| [claude-code-mcp-harness-research.md](claude-code-mcp-harness-research.md) | Claude Code skills/hooks/permissions/subagents, MCP SDK v2 |

The master prompt asked for separate files for indicators, ML, backtesting and metrics. They are merged into two files
(`strategy-landscape.md`, `ml-and-validation.md`) because the topics constrain each other and are easier to keep
consistent in one place.

## Evidence labels used everywhere

- **verified** — read in an official doc or observed by calling the API during this session.
- **literature** — claim from a named paper; the abstract/citation was checked this session, the full text was not re-read unless stated.
- **background** — well-known result recalled from training knowledge, **not** re-fetched this session. Treat as a lead.
- **hypothesis** — our own untested idea.
- **measured** — produced by code in this repo; see `research/experiments/`.

Nothing in this directory is evidence that any strategy is profitable.

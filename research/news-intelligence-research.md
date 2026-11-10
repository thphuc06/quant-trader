# News and web research subsystem

## Two different capabilities

| | Builder session (now) | Trader session (runtime) |
|---|---|---|
| Built-in `WebSearch` / `WebFetch` | **Available and used** for this research (verified by use on 2026-11-10) | Usually available in interactive Claude Code, but not guaranteed in every environment |
| MCP `search_web` / `fetch_web_page` / `search_crypto_news` | — | Provider-backed, optional API keys, results stored point-in-time |

The trader workflow uses whichever exists: MCP tools when a provider key is configured (results get an `evidence_id`
and are archived), otherwise the built-in tools, in which case Claude must quote URL and publication time itself.
With neither, news evidence is recorded as `not_available` and the decision can be at most `WATCH` when an event could matter.

## Providers

Interface: `SearchProvider.search(query, *, topic, recency, domains, limit) -> list[SearchHit]`.

| Provider | Role | Why |
|---|---|---|
| Tavily | Primary | One POST, a `news` topic that returns `published_date`, domain filters (TAV, verified) |
| Brave Search | Fallback | Independent index; simple GET (BRV, verified). Result date fields were not confirmed → `published_at` may be `unknown` |
| Exa | Not implemented | Comparable; add behind the same interface if needed |

Free-tier claims differ between sources (SRCH-CMP, low confidence), so nothing assumes a free tier. Keys are optional
environment variables (`TAVILY_API_KEY`, `BRAVE_API_KEY`). Without them the tools return
`status: "unavailable", reason: "not_configured"` — they never fabricate results.

Deferred, with reasons: macro calendar (no licensed source chosen), on-chain data, social sentiment (unreliable and
terms-of-service risk), exchange announcement feeds (need a stable official feed URL — not verified this session).

## Timestamps and point-in-time storage

Every stored item has `published_at` (as claimed by the source, may be null), `observed_at` (when our system first saw
it) and `ingested_at`. Backtests and event studies may only use `observed_at`. Because we have no historical archive,
**news cannot be backtested today**; the archive starts filling the day the system is used. We store URL, title, a
short snippet and our own hash — not article bodies (copyright).

## Source reliability

A categorical taxonomy, not a numeric score (a number would imply precision we do not have). Domain → type mapping is
in `knowledge/source_registry/sources.yaml`.

| Type | Examples | Use |
|---|---|---|
| `primary_official` | Exchange announcements, regulator and project sites | Can establish a fact |
| `reputable_secondary` | Established newsrooms | Fact if corroborated or clearly sourced |
| `specialist_data` | Market data vendors | Numbers, with vendor caveats |
| `social_community` | X, Reddit, Telegram | Lead only; never a fact |
| `unknown` | Everything else | Lead only |

Raw features kept alongside: source type, number of independent corroborating domains, whether a primary source was
found. A claim that would change a trade decision needs one primary source or two independent secondary ones;
otherwise it is reported as an unverified claim. Conflicts are reported, not resolved by picking the convenient side.

## Untrusted content and prompt injection

Web text is data. Controls in `src/trader/news/safe_fetch.py`:

- `http`/`https` only; no credentials in URLs; no non-standard ports.
- Host resolved before connecting; private, loopback, link-local (incl. cloud metadata `169.254.169.254`),
  multicast and reserved ranges are refused. Redirects are followed manually (max 3) with the same check each hop.
- 10 s timeout, 1 MB cap, text content types only, output truncated.
- HTML is reduced to text; scripts/styles dropped.
- Phrases that look like instructions to an AI ("ignore previous instructions", "you must call…", "system prompt")
  are flagged in `injection_flags`. The flag is a hint for the analyst, not a filter — detection by pattern is
  incomplete by nature.
- The server never puts secrets into outbound requests other than the provider's own API key header.

Residual risk: DNS rebinding between check and connect is mitigated by connecting to the resolved IP only when the
platform allows; otherwise it remains (documented in the threat model). The stronger protection is architectural: no
tool that web content could talk Claude into calling can move money without the risk engine and a human approval.

# Threat model

Scope: a single operator running Claude Code and the `trading` MCP server on their own machine.
Assets: exchange funds, API secrets, integrity of the risk policy and journal, the operator's decisions.

## Trust levels

| Actor / input | Trust |
|---|---|
| Operator at the keyboard | Trusted |
| Backend code in this repo at a reviewed commit | Trusted |
| Claude (main session and subagents) | **Semi-trusted**: well-intentioned, can be wrong, can be manipulated by content it reads |
| Web pages, news, search results, exchange text fields | Untrusted |
| Other software on the machine running as the same OS user | Out of scope (it can already read `.env`) |

## Threats and controls

| # | Threat | Control | Residual risk |
|---|---|---|---|
| T1 | Prompt injection in a web page tells Claude to trade, leak secrets or edit policy | Content flagged untrusted; no tool returns secrets; execution needs risk validation + approval; Claude's file tools denied on `.env*`; guard hook blocks policy edits in trader role | Claude could still be nudged into a *bad but policy-compliant* trade proposal. The human approval is the last check — read the intent before approving |
| T2 | Claude sizes or prices an order wrongly | Quantity is computed by the backend from equity, stop distance and policy; Claude cannot pass a quantity; exchange filters applied | Bad stop level chosen by Claude → wider/narrower risk than ideal, but always within the per-trade risk cap |
| T3 | Claude approves its own intent | Approval exists only as a CLI command that requires an interactive TTY and typed confirmation; guard hook and deny rules block the command from Claude's shell | **Not a hard boundary.** A sufficiently determined process running as the same user could write the approval row into SQLite directly. For LIVE, run the approval from a second OS account or machine, or accept this risk knowingly |
| T4 | Live trading enabled by accident | Four independent conditions (mode, flag, policy status, expiring TTY unlock); default PAPER; no fallback between environments | Operator deliberately bypassing all four |
| T5 | Duplicate orders after a timeout | Deterministic `clientOrderId`; unknown state → query before retry; intent stays `UNKNOWN` and blocks the symbol | Exchange accepting the order but never returning it in queries (not observed; would require manual check) |
| T6 | Secret leakage | `SecretStr`; never logged; redaction in audit; signed URLs never logged; `.env` git-ignored and read-denied to Claude | Operator pasting a key into chat; a key committed by hand |
| T7 | Withdrawal of funds | No withdraw/transfer code exists; docs require keys without withdrawal permission and with IP restriction | Key created with withdrawal enabled against instructions |
| T8 | SSRF via `fetch_web_page` | Scheme/port allowlist, DNS resolution with private-range rejection on every redirect hop, size/time caps | DNS rebinding between resolution and connection |
| T9 | Silent self-modification of strategy/policy | Lessons are files with `status: candidate`; promotion is a TTY-only CLI requiring a linked experiment; trader-role guard blocks edits to code/config/skills | Operator promoting lessons carelessly |
| T10 | Tampering with history to fit a narrative | Journal is append-only (SQL triggers reject UPDATE/DELETE) with a hash chain; `python -m trader verify-journal` | Someone with file access can rebuild the whole chain; keep off-machine backups if this matters |
| T11 | Stale or wrong market data drives a trade | Freshness limits in policy; unclosed candles dropped; gaps flagged; fail closed | Exchange serving wrong data |
| T12 | Overfitted strategy presented as an edge | Trial log, holdout lock, DSR/PBO, mandatory limitations block in reports | Researcher (human or model) running trials outside the registry |
| T13 | Unattended `claude -p` run executes tools without prompts | Documented as unsupported; execution still needs CLI approval outside PAPER | In PAPER a headless run could place simulated trades |
| T14 | Malicious dependency | Versions pinned in `requirements.lock`; small dependency set | Supply-chain compromise of a pinned release |
| T15 | Kill switch misuse | MCP can only *engage* it (stop new orders, optionally cancel resting orders); release is TTY-only; closing positions is never automatic | A position left open while the switch is engaged still carries market risk — its exchange-side stop remains |

## Fail-closed rules

No new position is opened when: the risk policy cannot be loaded or parsed; the quote is older than policy allows;
exchange filters are missing; the portfolio cannot be read; an intent for the symbol is in `UNKNOWN`; the kill switch
is engaged; the database is unavailable; any validation check errors instead of returning a result.

## Review triggers

Re-run this review when adding: futures/margin, a remote (HTTP) MCP transport, unattended operation, a new write tool,
or a second user.

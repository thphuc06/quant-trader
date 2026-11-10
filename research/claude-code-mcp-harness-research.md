# Claude Code / MCP harness research

Verified against current docs on 2026-11-10 (source IDs in [source-register.md](source-register.md)).

## Interactive session vs unattended runtime

- This repo targets an **interactive, supervised Claude Code session**. The human is present, sees permission prompts
  and approves trades.
- `claude -p` / the Agent SDK can run the same harness non-interactively, but `--bare` (the recommended scripted mode)
  "doesn't use your subscription login" and requires an API key from the Claude Console (CC-HEADLESS). An unattended
  24/7 trader is therefore an API-billed product decision, not something to build by reusing subscription credentials.
  We do not do that, and nothing here depends on it.
- Note from the same page: `-p` runs load project hooks and MCP servers **without** trust or approval prompts. Do not run
  `claude -p` in this repo with execution tools allowed.
- Consequence for design: nothing may assume Claude is awake. Stops are enforced by the exchange (demo/live) or
  re-evaluated against historical bars the next time a session opens (paper). There is no "Claude watches the
  position".

## Primitives and what each is used for

| Primitive | Loaded when | Used for |
|---|---|---|
| `CLAUDE.md` | Every session | Identity, golden rules, how to start. Kept short |
| Skills (`.claude/skills/<name>/SKILL.md`) | Description always visible; **body only on invocation** (CC-SKILL) | Procedures: `/trading-desk`, `/analyze-symbol`, `/post-trade-review`, `/strategy-research` |
| MCP tools | On call | Data, computation, risk, execution — anything that must be deterministic or audited |
| MCP resources | On request | Stable read-only context: policy, mode, universe |
| MCP prompts | User-invoked | Thin parameterised wrappers; the real procedure lives in skills |
| Subagents (`.claude/agents/*.md`) | When delegated | Isolated-context reviewers with a tool allowlist |
| Hooks | Deterministically, on events | Session banner, guard rails, audit |
| Permissions | Always | Least privilege; `ask` on execution tools |

Because skill bodies are not preloaded, `CLAUDE.md` tells every new session to run `/trading-desk` first, and that
skill names the reference files to open for each task.

## Hooks (CC-HOOK)

- Configured in `.claude/settings.json` under `hooks.<Event>[].hooks[]` with `type: "command"`.
- Input arrives as JSON on stdin (`tool_name`, `tool_input`, `hook_event_name`, …).
- Blocking a tool call: exit code 2 with the reason on stderr, or exit 0 with
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "…"}}`.
- `SessionStart` can inject `additionalContext` (≤ 10 000 chars).
- MCP tools match as `mcp__<server>__<tool>`; a server-wide matcher needs `mcp__trading__.*`.

Hooks we ship (all stdlib Python, so they run without the virtualenv):

| Event | Script | Purpose |
|---|---|---|
| `SessionStart` | `session_start.py` | Print role, trading mode, live lock, policy version, kill-switch state, open intents |
| `PreToolUse` (Edit/Write/Bash/PowerShell) | `guard.py` | In trader role: block edits outside `state/` and the lesson/casebook folders; block shell commands that approve trades, unlock live, read `.env`, or call Binance order endpoints |
| `PostToolUse` (`mcp__trading__.*`) | `audit.py` | Append a redacted line to `data/audit/claude_tool_calls.jsonl` |
| `PreCompact` | `pre_compact.py` | Remind Claude to persist a session summary to `state/session-summary.md` |

`Stop` hooks were considered and skipped: open intents are already shown at session start and expire by TTL on the
server; a blocking Stop hook mostly produces loops.

**Hooks are a convenience layer, not the security boundary.** A hook runs with the user's privileges in the same
checkout and can be edited by anyone who can edit the repo. Every rule that matters is enforced again inside the
Python backend (risk engine, approval ledger, live lock).

## Permissions (CC-PERM)

- `deny` beats `ask` beats `allow`; a hook returning `allow` cannot override a deny/ask rule.
- `Read(.env)` style rules block Claude's file tools from reading secrets.
- MCP rules: `mcp__trading__get_*` style globs are valid in `allow` after a literal server prefix.
- A committed `.claude/settings.json` cannot approve its own MCP server in an untrusted folder (CC-MCP); the user
  approves `trading` once at first launch.

Shipped policy: read-only MCP tools allowed; `create_trade_intent`, `execute_approved_trade`, `cancel_trade_intent`,
`activate_kill_switch`, `submit_post_trade_review` set to **ask**; `.env*` reads denied; `bypassPermissions` disabled.

## Subagents (CC-AGENT)

Two only, both read-only, neither has `Agent` (no nesting) nor execution tools:

- `news-researcher` — web search/fetch + knowledge tools; returns cited, timestamped findings.
- `risk-reviewer` — market/risk read tools; argues against the thesis.

A separate "market-analyst" agent was dropped: the main session already does that job, and splitting it would only
hide the quantitative evidence from the reasoning that uses it.

## MCP server (MCP-PY, MCP-MIG, MCP-TEST)

- SDK `mcp==2.3.0` (pinned). **v2 renamed `FastMCP` to `MCPServer`** (`from mcp.server.mcpserver import MCPServer`);
  code written from v1 memory fails with `ModuleNotFoundError`. Decorators (`@mcp.tool()`, `@mcp.resource()`,
  `@mcp.prompt()`) keep v1 signatures; result fields are snake_case (`structured_content`, `is_error`).
- Transport: **stdio**, started by Claude Code from `.mcp.json`. Streamable HTTP is unnecessary for a local
  single-user desk and would add an authentication problem.
- Tests use the in-memory `Client(server, raise_exceptions=True)`.
- Output budget: Claude Code warns above 10k tokens and caps at 25k by default, so list tools are bounded (≤ 200
  candles, ≤ 10 search results, truncated page text).
- stdout belongs to the protocol; all logging goes to stderr or files.

### Tool design rules adopted

1. A few task-shaped tools instead of one per indicator (`get_market_snapshot` returns features, regime and signals together).
2. Every result uses one envelope: `status` (`ok | degraded | stale | unavailable | rejected | error`), `as_of` (UTC), `evidence_id`, `data`, `warnings`.
3. `evidence_id` is stored server-side; a decision report may only cite IDs that exist, which stops invented evidence.
4. Mutating tools take an `idempotency_key`; none accepts a quantity, a raw order payload, or a URL to an exchange.
5. Web content is returned inside a field marked `untrusted_content: true` with instruction-like phrases flagged.

## Upgrade checks

Run `python -m trader doctor` after upgrading Claude Code or `mcp`: it imports the server, lists tools, and verifies
the hook scripts parse. Re-read the hooks and permissions pages when Claude Code changes major version — event names
and decision formats have changed before.

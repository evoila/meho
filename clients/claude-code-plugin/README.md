<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `meho` — Claude Code plugin

One installable, versioned unit that wires a Claude Code session to a MEHO
backplane and loads the MEHO-first operating discipline. It replaces the
manual onramp — hand-wiring MCP plus copy-merging the Layer-2 `CLAUDE.md`
template and re-pulling it on every MEHO release — for Claude Code consumers.

## Install

```bash
claude plugin marketplace add evoila/meho
/plugin install meho@meho
```

`evoila/meho` is both the marketplace (`.claude-plugin/marketplace.json` at
the repo root) and the host of this inline plugin
(`./clients/claude-code-plugin`). During development you can point the
marketplace at a local checkout instead of `evoila/meho`.

## What it installs

- **`.mcp.json`** — a stdio MCP server named `meho` that launches the
  `mcp-remote` shim through `bin/meho-mcp-remote`. MEHO is internal-only and
  never publicly exposed; the shim runs on your own VPN-connected machine and
  forwards Streamable-HTTP calls to the backplane's `/mcp` endpoint. This
  stdio-shim form is deliberate until the realm baseline supports CIMD;
  flipping to native HTTP MCP is a later, separate change.
- **`skills/`** — the Layer-2 routing discipline as model-invoked skills,
  namespaced `meho:<skill>`: `meho:prefer-meho`, `meho:knowledge`,
  `meho:memory`, `meho:operations`, `meho:broadcast`.
- **`hooks/`** — deterministic reflex hooks (`hooks/hooks.json` + scripts
  under `bin/`) that the harness runs without model initiative: a
  session-start digest and advisory announce/report reminders. See
  [Reflex hooks](#reflex-hooks).

## Configuration — no edits inside the installed plugin

`bin/meho-mcp-remote` reads everything from the operator's environment (or an
optional config file), so you never edit a file inside the installed plugin
to point it at a tenant:

| Variable | Purpose |
|---|---|
| `MEHO_MCP_URL` | Full MCP endpoint, e.g. `https://meho.internal.example/mcp`. Takes precedence. |
| `MEHO_INSTANCE` | Backplane base URL, e.g. `https://meho.internal.example`. The wrapper appends `/mcp`. |
| `MEHO_CA_CERT` | Path to an internal-CA bundle; exported as `NODE_EXTRA_CA_CERTS` for the Node-based shim. Only needed on internal-CA deploys. |
| `MEHO_MCP_CLIENT_ID` | Pre-registered public OAuth client id the shim presents to Keycloak (default `meho-mcp`). Override only if the realm registers the client under a different name. |
| `MEHO_PLUGIN_CONFIG` | Override the config-file location (default `${XDG_CONFIG_HOME:-~/.config}/meho/plugin.env`). |

Set the variables in your shell profile, or drop them in the config file:

```bash
# ~/.config/meho/plugin.env
MEHO_INSTANCE="https://meho.evba.lab"
# MEHO_CA_CERT="/etc/ssl/internal-ca.pem"   # only on internal-CA deploys
```

With either configured, the `meho` MCP server connects on the next Claude
Code session — zero edits inside the installed plugin.

### Prerequisites

- A machine on the internal network / VPN that can reach the backplane.
- Node.js with `npx` available (the shim runs the smoke-tested
  `npx -y mcp-remote@0.1.38`).
- `mcp-remote` performs the OAuth 2.1 + PKCE handshake against the realm. The
  shim passes `--static-oauth-client-info '{"client_id": "meho-mcp"}'` (overridable
  via `MEHO_MCP_CLIENT_ID`) so it presents the pre-registered public client
  rather than attempting RFC 7591 Dynamic Client Registration — which MEHO's
  Keycloak realm blocks (default Trusted Hosts policy → `403 "Host not
  trusted"`). The `meho-mcp` client must already exist on the realm; see
  `docs/cross-repo/mcp-client-setup.md`.
- If the backplane URL is not configured, `bin/meho-mcp-remote` exits with a
  message naming the variables to set.

## Layer-2 skills → template sections

The skills migrate the routing rules from the
[`docs/examples/consumer-onboarding/CLAUDE.md`](../../docs/examples/consumer-onboarding/CLAUDE.md)
template. The template stays authoritative for non-plugin clients (Cline,
Continue, CI bots); Claude Code consumers get the same discipline as skills:

| Template section | Skill |
|---|---|
| Intro + Connection + What stays local + When unavailable + Versioning | `meho:prefer-meho` |
| Knowledge base | `meho:knowledge` |
| Memory | `meho:memory` |
| Targets + Connectors + Audit | `meho:operations` |
| Live awareness + Broadcast discipline | `meho:broadcast` |

## Reflex hooks

Skills and MCP descriptions are advisory — the model chooses whether to
follow them. Hooks are deterministic: the Claude Code harness runs them, no
model initiative required. `hooks/hooks.json` wires three reflexes, every one
**fail-open** (missing `meho` CLI, expired login, no VPN, or timeout ⇒ silent
no-op, exit 0 — the session never breaks) and **warn-only** (no hook denies or
blocks a tool call; enforcement with teeth is the server-side announce gate's
job):

| Event | Script | Behaviour |
|---|---|---|
| `SessionStart` | `bin/session-start.sh` | Fetches a compact digest — recent tenant activity (the durable form of the broadcast window), scoped memory, and recent knowledge — via the `meho` CLI and prints it to stdout. Claude Code injects a SessionStart hook's stdout as session context, so read-before-start is automatic instead of a discipline the model must remember. |
| `PreToolUse` on `call_operation` | `bin/pre-call-operation.sh` | If the session hasn't announced intent yet, emits a one-time advisory reminder naming `broadcast_announce`, returned as `hookSpecificOutput.additionalContext` (no `permissionDecision`, so the normal permission flow proceeds). |
| `PostToolUse` on `broadcast_announce` | `bin/post-announce.sh` | Records that the session announced/reported, so the `PreToolUse` and `Stop` reminders fall silent. |
| `Stop` | `bin/stop-report.sh` | If the session invoked `call_operation` but never announced/reported, emits a one-line report-on-completion reminder as `additionalContext`. This *continues* the turn once so Claude can act on it (Stop `additionalContext` does not let the turn end — it runs under the same loop protections as `decision: block`). It is bounded to a single nudge: the hook no-ops when `stop_hook_active` is `true` and writes a once-per-session marker, then lets the session stop. It never exits 2 (which would hard-block the stop). |

The digest reads `meho status` (gate), `meho audit recent`, `meho list`
(memory), and `meho kb list`, each bounded by `timeout`/`gtimeout` when
present. The live broadcast feed (`meho status --watch`) is an SSE stream and
unsuitable for a hook that must return promptly, so the digest uses the
audit log — every MEHO op writes an audit row and emits a broadcast event, so
`meho audit recent` is the bounded, non-streaming read of the same window.
These hooks **enforce** the broadcast discipline the server-assembled tenant
preamble already states; they don't restate it.

### Scoped matcher form (the gotcha)

A tool from a **plugin-bundled** MCP server does not get the bare
`mcp__<server>__<tool>` name. It is scoped with the plugin name:

```
mcp__plugin_<plugin-name>_<server-name>__<tool>
```

Both the plugin and its bundled MCP server are named `meho`, so
`call_operation` is `mcp__plugin_meho_meho__call_operation` and
`broadcast_announce` is `mcp__plugin_meho_meho__broadcast_announce`. A bare
`mcp__meho__call_operation` matcher **never fires** for a plugin-bundled
server. Add a `.*` suffix (`mcp__plugin_meho_meho__.*`) when a server-wide
match is wanted; the matchers here target the two exact tool names.

Session state (`used` / `announced` / `reminded` / `stop_reminded`) lives in
per-session marker files under `${TMPDIR:-/tmp}/meho-plugin-hooks`, keyed by
`session_id`, so the `PreToolUse` announce reminder (`reminded`) and the `Stop`
report reminder (`stop_reminded`) each fire at most once per session.

## Out of scope

- CIMD / native-HTTP MCP migration of `.mcp.json`.

## References

- [Claude Code plugins](https://code.claude.com/docs/en/plugins.md)
- [Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces.md)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks.md) — SessionStart
  stdout-as-context, `PreToolUse` / `PostToolUse` / `Stop` semantics, the
  plugin-scoped MCP tool-name form, and `${CLAUDE_PLUGIN_ROOT}`.
- [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
  — the `client_id` gap that motivates the stdio-shim form, and the
  internal-only posture.

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

To get a newer plugin version later, run `claude plugin update meho@meho`,
then restart Claude Code. Claude Code does not update plugins from this
marketplace on its own unless you turn on auto-update for it in `/plugin`.
Each change that users need must also raise the `version` in this
plugin's `.claude-plugin/plugin.json` and in the repo-root
`.claude-plugin/marketplace.json`. Without that, `claude plugin update`
keeps the copy you already have.

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
| `MEHO_MCP_SCOPES` | Space-separated OAuth scopes the shim requests (default `mcp:read mcp:execute offline_access`: the working surface plus a [login that lasts](#how-long-the-login-lasts)). It replaces the whole list. Set it to opt into [elevated operator mode](#elevated-operator-mode-opt-in). Empty or whitespace-only falls back to the default. |
| `MEHO_MCP_LOGIN_TIMEOUT` | Seconds the shim waits for the connection, including a browser login, before it stops (default `300`). See [When the login expires or is not finished](#when-the-login-expires-or-is-not-finished). |
| `MEHO_PLUGIN_CONFIG` | Override the config-file location (default `${XDG_CONFIG_HOME:-~/.config}/meho/plugin.env`). |

Set the variables in your shell profile, or drop them in the config file:

```bash
# ~/.config/meho/plugin.env
MEHO_INSTANCE="https://meho.evba.lab"
# MEHO_CA_CERT="/etc/ssl/internal-ca.pem"   # only on internal-CA deploys
```

With either configured, the `meho` MCP server connects on the next Claude
Code session — zero edits inside the installed plugin.

### Elevated operator mode (opt-in)

By default a session lists only the **working surface** — do-work and
coordinate: status, connector/operation discovery, `call_operation` /
`preview_operation` / `result_query`, knowledge, memory, the broadcast
trio, target/topology reads, and the runbook run family. Everything an
agent or a plain operator needs to get work done.

`MEHO_MCP_SCOPES` is the deliberate opt-in to **operator mode**. Add the
elevated scope to the request:

```bash
MEHO_MCP_SCOPES="mcp:read mcp:execute offline_access mcp:admin"
```

Keep `offline_access` in the list, or the login ends after the realm's SSO
idle limit (see [How long the login lasts](#how-long-the-login-lasts)).

An elevated session additionally lists the **operator planes**: the
agents / principals registry and grants, connector lifecycle, broadcast
overrides, the scheduler, sensors, runbook template authoring, and the
approvals **read** views (`meho_approvals_list` / `meho_approvals_get`).

Elevation **never** exposes the human-only verbs. `meho_approvals_approve`,
`meho_approvals_reject`, and `meho_agents_grant_elevate` have no MCP path
under any scope — approving a parked operation and granting elevation are
human decisions made through the console or CLI only (#3155). A model
holding the approve button would collapse the four-eyes gate, so the button
is not on the wire.

**Realm prerequisite.** `mcp:admin` must be offered as an **optional**
(requestable, non-default) client scope on the realm's `meho-mcp` client.
If it is not, Keycloak refuses the whole login: it answers `invalid_scope`
("Invalid scopes: …") and issues no token. Keycloak does not drop a scope
it does not offer. So set `MEHO_MCP_SCOPES` with `mcp:admin` only after the
realm offers it.

### How long the login lasts

The shim asks Keycloak for `offline_access` by default. Keycloak then
issues an offline refresh token, and the login lasts until you have not
used MEHO for the `meho-mcp` client's offline idle limit. The realm docs
recommend 48 hours
([Keycloak realm setup](../../docs-site/install/keycloak-realm.md)).
`mcp-remote` keeps the login in a file under `~/.mcp-auth` that only your
user can read.

Without `offline_access`, the login ends at the realm's SSO idle limit,
which is 30 minutes by default. The next MEHO call then needs a new browser
login.

The realm must offer `offline_access` as an **optional** client scope on
`meho-mcp` (`meho admin keycloak bootstrap-clients` sets this up). If it
does not, Keycloak refuses the login with `invalid_scope`.

### Prerequisites

- A machine on the internal network / VPN that can reach the backplane.
- Node.js with `npm` available (the shim vendors the smoke-tested
  `mcp-remote@0.1.39` from the committed `package-lock.json` with
  `npm ci --omit=dev` on first launch, then runs the lock-installed local
  entry — no floating `npx -y` transitive install). The integrity-checked
  tree is cached under `${XDG_CACHE_HOME:-~/.cache}/meho/mcp-remote`
  (override with `MEHO_PLUGIN_CACHE`) and re-vendored only when the committed
  lockfile changes.
- `mcp-remote` performs the OAuth 2.1 + PKCE handshake against the realm. The
  shim passes `--static-oauth-client-info '{"client_id": "meho-mcp"}'` (overridable
  via `MEHO_MCP_CLIENT_ID`) so it presents the pre-registered public client
  rather than attempting RFC 7591 Dynamic Client Registration — which MEHO's
  Keycloak realm blocks (default Trusted Hosts policy → `403 "Host not
  trusted"`). The `meho-mcp` client must already exist on the realm; see
  `docs/cross-repo/mcp-client-setup.md`.
- If the backplane URL is not configured, `bin/meho-mcp-remote` exits with a
  message naming the variables to set.

## First connect — raise the MCP startup timeout

The **first** time a fresh machine connects, `mcp-remote` has to complete the
full Keycloak OAuth 2.1 + PKCE login before the `meho` server reports ready. It
opens a browser tab for that login — **behind the terminal window** — and
Claude Code's default **30-second MCP startup timeout** gives up on the server
if the browser login has not finished in time. On a vanilla `claude` first run
the login reliably loses that race.

Start the **first** session with a longer startup budget:

```bash
MCP_TIMEOUT=120000 claude
```

Then switch to the browser tab that opened **behind** your terminal and
complete the Keycloak login. `mcp-remote` caches the tokens under
`~/.mcp-auth`, so **every later session connects instantly** — the raised
timeout is only needed for that one first connect (once per fresh machine, or
again after you clear `~/.mcp-auth`).

**Why the plugin can't set this for you.** `MCP_TIMEOUT` is the MCP *startup*
timeout, and Claude Code reads it from **its own process environment** at
launch — before it spawns any server. The plugin's `.mcp.json` can only set the
*server child's* environment, which the CLI never consults for the startup
deadline, so no `.mcp.json` key raises it. (The per-server `timeout` field that
`.mcp.json` *does* accept is a **tool-execution** timeout — a per-call
wall-clock limit that overrides `MCP_TOOL_TIMEOUT` — not the startup timeout, so
it does not help here.) The variable has to live in the shell that launches
`claude`. See the
[Claude Code MCP docs](https://code.claude.com/docs/en/mcp.md).

## When the login expires or is not finished

The shim runs `mcp-remote` through a small login guard
(`lib/login-guard.mjs`). The guard makes sure a MEHO call never hangs because
of a login:

- **The login expired during a session.** This happens after the offline idle
  limit (48 hours with no use, see
  [How long the login lasts](#how-long-the-login-lasts)) or when an admin
  ended the session in Keycloak. The next MEHO call fails at once with:
  "Your MEHO login has expired. Run /mcp, pick meho, and reconnect to log in
  again." Then the shim stops, and `/mcp` shows `meho` as failed. Do what the
  message says; the reconnect opens the browser login. If Claude Code stops
  waiting before you finish it, finish it anyway and reconnect once more. A
  browser tab with the MEHO login may also open when the call fails. You can
  close it.
- **Nobody finishes a browser login.** The shim stops after
  `MEHO_MCP_LOGIN_TIMEOUT` seconds (default 300, so 5 minutes) without a
  connection. This frees the local callback port, so the next session can log
  in. The MCP log then has a line starting with `meho plugin:` that says what
  happened.
- **Two sessions need a login at the same time.** The second session waits for
  the first login and uses it when it is done. If it is not done in time, the
  second session stops with the same `meho plugin:` message. It does not
  crash. Claude Code may stop waiting for the second session earlier (its
  startup timeout); then reconnect it with `/mcp` after the login is done.

`mcp-remote` keeps saved logins in `~/.mcp-auth/mcp-remote-0.1.38/` (the
pinned `mcp-remote@0.1.39` names the folder after its previous version). The
folder changed with this version of the plugin, so expect one new browser
login after you update.

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
| `PreToolUse` on `call_operation` | `bin/pre-call-operation.sh` | If the session hasn't announced intent yet, emits a one-time advisory reminder naming `meho_broadcast_announce`, returned as `hookSpecificOutput.additionalContext` (no `permissionDecision`, so the normal permission flow proceeds). |
| `PostToolUse` on `meho_broadcast_announce` | `bin/post-announce.sh` | Records that the session announced/reported, so the `PreToolUse` and `Stop` reminders fall silent. |
| `Stop` | `bin/stop-report.sh` | If the session invoked `call_operation` but never announced/reported, emits a one-line report-on-completion reminder as `additionalContext`. This *continues* the turn once so Claude can act on it (Stop `additionalContext` does not let the turn end — it runs under the same loop protections as `decision: block`). It is bounded to a single nudge: the hook no-ops when `stop_hook_active` is `true` and writes a once-per-session marker, then lets the session stop. It never exits 2 (which would hard-block the stop). |

The digest reads `meho status` (gate), `meho audit recent`, `meho list`
(memory), and `meho kb list`, each bounded by `timeout`/`gtimeout` when
present. The live broadcast feed (`meho status --watch`) is an SSE stream and
unsuitable for a hook that must return promptly, so the digest uses the
audit log — every MEHO op writes an audit row and emits a broadcast event, so
`meho audit recent` is the bounded, non-streaming read of the same window.
The recent-activity window fetches a wide page and drops rows written under
the reserved `__sensor__` principal (the checks plane's continuous sensor
executions) *before* truncating to the display budget, so on a checks-enabled
tenant those machine heartbeats can't evict the operator/agent activity the
window exists to surface.
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
`meho_broadcast_announce` is `mcp__plugin_meho_meho__meho_broadcast_announce`
(mind the `meho_` tool-prefix — the scoped name embeds the exact registered
tool name, so the broadcast tools carry `meho_` twice). A bare
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

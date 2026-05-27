<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# R4 — Local Claude as triage: the operator recipe

> Step-by-step setup for the [R4 reference pattern](./README.md):
> operator's Claude Code talks MCP to a MEHO backplane under the
> operator's own Keycloak identity, paired with a 24/7 hosted
> cheap-tier agent that pre-triages broadcast events and writes
> "interesting" handoffs into memory.

This guide is **composition only** — it wires existing MEHO
primitives. No new server-side endpoint is introduced. If you're
new to MEHO's MCP surface, read
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
first — it covers the realm-side audience mapper and the four
walls of the auth onramp; this guide refers back to it rather
than duplicating it.

## What you'll have at the end

- A hosted agent definition named `r4-alert-triage` with
  `model_tier=fast`, attached to a `*/15 * * * *` cron trigger
  that fires the triage loop against your tenant's broadcast feed
  every 15 minutes.
- A local Claude Code project whose `.mcp.json` points at your
  MEHO instance under the operator's own Keycloak token.
- A working handoff channel: when the hosted agent flags an event
  as interesting, it lands as a tenant-scoped memory entry with
  slug `r4-handoff-<event_id>` and tag `r4-triage-handoff`. The
  local Claude reads that scope via the `search_memory` MCP tool
  on every "what's interesting?" prompt.

## Prereqs

You have, in this order:

1. **A running MEHO backplane** at some hostname (we'll use
   `https://meho.example.com`). Verify with
   `curl https://meho.example.com/healthz`.
2. **A Keycloak realm wired per
   [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).**
   You should already have an MCP client with the 5 mappers, the
   4 default scopes, and an audience mapper for `MCP_RESOURCE_URI`.
3. **The MEHO CLI installed and authenticated:**
   ```bash
   meho version           # > meho/v0.2.x ...
   meho login https://meho.example.com  # OAuth device flow
   ```
4. **Your operator account has role `operator` or `tenant_admin`**
   bound on the tenant. If your `tools/list` over MCP returns an
   empty list, the role is below `read_only` — see
   [`docs/cross-repo/mcp-client-setup.md` § `tools/list` returns an empty list](../../docs/cross-repo/mcp-client-setup.md#toolslist-returns-an-empty-list).
5. **An agent principal for the cheap-tier triage agent.**
   `meho agent-principal register r4-alert-triage` is the verb
   ([G11.2-T1 #815](https://github.com/evoila/meho/issues/815)).
   The agent principal's `client_credentials` token is what the
   scheduler will impersonate when the cron fires; the principal's
   JWT `sub` is `agent:r4-alert-triage` (clientId format from
   [`cli/internal/cmd/agent-principal/register.go`](../../cli/internal/cmd/agent-principal/register.go)).

## Step 1 — Create the hosted cheap-tier agent definition

The agent definition is in
[`agent.alert-triage.json`](./agent.alert-triage.json). The
toolset subobject is split out into
[`toolset.json`](./toolset.json) because the CLI's
`--toolset @<path>` reads the **whole file** as the flag value —
not just one key. Passing the agent-definition JSON directly to
`--toolset` would make the entire object the toolset, which the
server rejects. Always pass the split subobject file.

```bash
meho agent create r4-alert-triage \
  --identity-ref "agent:r4-alert-triage" \
  --model-tier fast \
  --turn-budget 8 \
  --system-prompt "$(jq -r .system_prompt examples/r4-local-claude/agent.alert-triage.json)" \
  --toolset "@examples/r4-local-claude/toolset.json"
```

> The `--toolset` flag accepts inline JSON, `@<path>` (file
> contents), or `@-` (stdin), per the CLI's `loadJSONObjectFlag`
> in [`cli/internal/cmd/agent/agent.go`](../../cli/internal/cmd/agent/agent.go).
> The contents are parsed as a JSON object and forwarded as the
> agent definition's `toolset` field. The split file
> [`toolset.json`](./toolset.json) keeps the GUIDE's command line
> matching production behaviour.

Verify:

```bash
meho agent list | grep r4-alert-triage
# expect: r4-alert-triage  model=fast  enabled=true  ...
```

The agent's system prompt is the **decision logic** for "interesting
vs noise" — read it in
[`agent.alert-triage.json`](./agent.alert-triage.json) before you
ship the agent live; tune the thresholds (3x baseline crossing, the
list of write op-classes, etc.) to your tenant's traffic shape.

## Step 2 — Wire the cron trigger

[`scheduler.cron.json`](./scheduler.cron.json) is the
`ScheduledTriggerCreate` payload. The default fire-cadence is
`*/15 * * * *` (every 15 minutes UTC) — the cheap-tier round trip
is cheap but not free, and 15 minutes is a comfortable floor for
human-attention latency on alert-triage. Move to `*/5 * * * *` if
your tenant has high broadcast volume; only drop to `* * * * *`
on dedicated noisy tenants where the latency floor matters more
than the budget burn.

> **Why cron and not `kind=event`?** The scheduler's `kind=event`
> dispatch path matches `event_filter` against drained events
> ([`backend/src/meho_backplane/events/drain.py`](../../backend/src/meho_backplane/events/drain.py)),
> but the **junction-table populate path** (`scheduled_trigger`
> rows of `kind='event'` linked to drained events) is not yet
> wired in v0.2 — the dispatch function is a no-op subscriber
> match. Cron is the path that actually fires the agent today.
> A future minor version flips the event-kind path live; the
> upgrade is one field on the trigger row, not a new agent or
> guide.

The trigger's `--inputs` is split out into
[`inputs.json`](./inputs.json) for the same reason the toolset
file is — `--inputs @<path>` reads the whole file as the inputs
value. Resolve your `agent_definition_id` and create the trigger:

```bash
AGENT_ID=$(meho agent show r4-alert-triage --json | jq -r .id)

meho scheduler create \
  --kind cron \
  --agent-definition "$AGENT_ID" \
  --cron-expr "$(jq -r .cron_expr examples/r4-local-claude/scheduler.cron.json)" \
  --timezone   "$(jq -r .timezone   examples/r4-local-claude/scheduler.cron.json)" \
  --inputs     "@examples/r4-local-claude/inputs.json" \
  --in-flight-policy fail_into_audit
```

> The `--inputs` flag forwards the file's JSON as the agent run's
> input subobject per the scheduler contract — see
> [`docs/codebase/scheduler.md`](../../docs/codebase/scheduler.md).

Verify:

```bash
meho scheduler list | grep r4-alert-triage
# expect a row with kind=cron, next_fire_at within ~15 minutes
```

Confirm the cheap-tier ran (the scheduler fires under the agent
principal sub `agent:r4-alert-triage`, so filter the audit log on
that principal — `meho agent` has per-run lookup
(`meho agent run-status <handle>`) but no "list runs of this
agent" verb; auditing the principal is the equivalent path):

```bash
meho audit query --principal agent:r4-alert-triage --limit 5 --json \
  | jq '.rows[] | {ts, op_id, result_status}'
```

A successful triage run lands one or more rows with
`op_id=meho.broadcast.recent`, optionally followed by
`op_id=add_to_memory` (the latter ONLY when the cheap-tier
decided some event was interesting). A `result_status=denied` on
`add_to_memory` is the most common first-time failure — the
agent principal needs the grant set up in Step 4 below.

## Step 3 — Wire the local Claude Code's `.mcp.json`

The example is in [`mcp.json.example`](./mcp.json.example). It
ships **two variants** — pick exactly one for your environment.

### Variant A — direct HTTP (Claude.ai / Inspector / Cline / Continue / CIMD)

If your MCP client exposes a `client_id` field OR your Keycloak
realm runs CIMD (see
[`docs/cross-repo/mcp-client-setup.md` § Claude Code (HTTP MCP) and Cursor — `.mcp.json` `client_id` limitation](../../docs/cross-repo/mcp-client-setup.md#claude-code-http-mcp-and-cursor--mcpjson-client_id-limitation)),
the direct HTTP shape works. Drop into the operator's local repo
as `.mcp.json`:

```json
{
  "mcpServers": {
    "meho": {
      "type": "http",
      "url": "https://meho.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${MEHO_MCP_TOKEN}"
      }
    }
  }
}
```

Export the token in the operator's shell. The MEHO CLI stores
tokens in the OS keyring when available; when not (headless
hosts, CI containers, or with `MEHO_KEYRING_DISABLE=1`), it falls
back to a 0600-mode credentials file at
`$XDG_CONFIG_HOME/meho/credentials.json` (defaults to
`$HOME/.config/meho/credentials.json`). See
[`cli/internal/auth/store.go`](../../cli/internal/auth/store.go)
for the resolution logic.

There is **no `meho login --print-token` verb today**; extract
the token from whichever backend stored it:

```bash
# File backend (headless / MEHO_KEYRING_DISABLE=1):
export MEHO_MCP_TOKEN="$(jq -r '
  .entries
  | to_entries[]
  | select(.key | endswith("https://meho.example.com"))
  | .value.access_token
' "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json")"

# Keyring backend (macOS Keychain / Secret Service / Wincred):
# The credentials file does not exist; query the OS keyring
# directly. On macOS:
#   security find-generic-password -s meho -a https://meho.example.com -w
# On Linux:
#   secret-tool lookup service meho user https://meho.example.com
# Or set MEHO_KEYRING_DISABLE=1 and re-run `meho login` to force
# the file backend, then use the jq snippet above.
```

> Token rotation is per-deployment. The default Keycloak access
> token TTL is short (5–15 minutes); export the variable in a
> rotating helper, or use the Custom Connector / CIMD path which
> manages refresh internally. The `refresh_token` is stored
> alongside the access token in the same file/keyring entry
> (`StoredToken.RefreshToken` in
> [`cli/internal/auth/store.go`](../../cli/internal/auth/store.go))
> for the v0.2 `meho refresh` verb once it ships.

### Variant B — stdio shim via `mcp-remote` (everything else)

When the local client is Claude Code's stdio-MCP path on a non-CIMD
realm:

```json
{
  "mcpServers": {
    "meho": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://meho.example.com/mcp",
        "--header",
        "Authorization: Bearer ${MEHO_MCP_TOKEN}"
      ]
    }
  }
}
```

`mcp-remote` is the official stdio↔HTTP MCP transport shim. The
Claude Code session spawns `npx mcp-remote ...` on stdio; the shim
holds the token and translates JSON-RPC frames to Streamable HTTP.
Token rotation is the same as Variant A.

## Step 4 — Scope grants for the agent principal (tenant_admin)

The cheap-tier agent's write target is **memory `scope="tenant"`**
(per [`agent.alert-triage.json`](./agent.alert-triage.json)). Per
the RBAC matrix in
[`backend/src/meho_backplane/memory/rbac.py`](../../backend/src/meho_backplane/memory/rbac.py)
`MemoryRbacResolver.can_write`, tenant-scope writes require
**either** the principal to hold `tenant_admin`, **or** a per-
principal grant for `add_to_memory` via the G11.2 grant table
([G11.2-T6 #819](https://github.com/evoila/meho/issues/819)).

The deployer issues those grants on the agent principal at
install time. The grant verb family is **`meho agent grant`**,
not `meho agent-principal grant` — grants are properties of the
(principal, op-pattern, target) triple, not the principal itself.

```bash
# Grant the cheap-tier the minimal scope it needs. The principal
# sub follows the clientId convention `agent:<name>` set by
# `meho agent-principal register`.
PRINCIPAL="agent:r4-alert-triage"

# Read: pull recent broadcasts.
meho agent grant create \
  --principal "$PRINCIPAL" \
  --op meho.broadcast.recent \
  --verdict auto-execute

# Read: search memory (for re-triage / dedupe checks).
meho agent grant create \
  --principal "$PRINCIPAL" \
  --op search_memory \
  --verdict auto-execute

# Write: add a tenant-scoped handoff entry. Without this grant,
# add_to_memory at scope="tenant" returns 403 PermissionDenied;
# the prompt's whole pattern stops working.
meho agent grant create \
  --principal "$PRINCIPAL" \
  --op add_to_memory \
  --verdict auto-execute
```

> **Why grant the agent, not widen the operator's role:** the
> agent principal needs *exactly* the read-broadcast-feed +
> write-handoff-memory grants to do its job — and nothing else.
> The G11.2 grant table is where that minimal-scope is bound,
> not the operator's `tenant_role`. The operator's role stays
> coarse on purpose; the agent's grants are fine.

Verify the grants landed:

```bash
meho agent grant list --principal "$PRINCIPAL"
# expect three rows: meho.broadcast.recent / search_memory / add_to_memory
# all with verdict=auto-execute, target_scope=*  (no target narrowing)
```

### Operator's own session role

The local Claude session inherits the **operator's** Keycloak
token, which carries the operator's `tenant_role` claim. That
role binds **every** call the session makes via MCP, including
the ones the local model decides to issue without asking:

- `read_only` — read tools work (`meho.status`, `search_memory`,
  `search_knowledge`, `meho.broadcast.recent`); write tools 403.
- `operator` — adds tool-call execution: `meho.agents.run`,
  `meho.connector.*` reads, write tools the per-(agent_principal,
  op_class, target) grant table allows.
- `tenant_admin` — adds the admin surface (agent definitions,
  scheduler triggers, broadcast overrides). Note `add_to_memory`
  at `scope="tenant"` is in this lane — the operator's local
  session reading the handoff via `search_memory` does **not**
  need `tenant_admin`, only the read path (tenant scope is
  readable by every operator in the tenant per
  [`MemoryRbacResolver.can_read`](../../backend/src/meho_backplane/memory/rbac.py)).

**Recommended posture for the operator's local session:**

- Day-to-day triage runs as **`operator`**. The session asks
  "what's interesting?", reads the handoff memory entries, drills
  down into broadcasts and topology, and runs known-good ops.
- For change-class ops (rolling a credential, draining a node), the
  operator either: (a) flips their session token to a
  `tenant_admin` one with a short TTL via a second `meho login`
  invocation, or (b) escalates to a hosted agent run gated by the
  R2 operator-approval flow ([sibling task #1082](https://github.com/evoila/meho/issues/1082)).

## Step 5 — Verify the alerting handoff end-to-end

The verification chain has four steps:

1. **Hosted agent fires.** From Step 2, you already saw audit rows
   for the principal `agent:r4-alert-triage`.
2. **Hosted agent writes a handoff entry** (when an event is
   interesting). On a quiet tenant you can force the path by
   running a write op that classifies as `interesting` per the
   prompt (e.g. mint a credential against a production target).
   There is **no `meho broadcast announce` verb today**; broadcast
   events are emitted by the dispatcher as a side effect of real
   audit rows (per
   [`backend/src/meho_backplane/mcp/handlers.py`](../../backend/src/meho_backplane/mcp/handlers.py)
   `compute_effective_broadcast_detail`). After the cron tick, list
   the tenant-scope memory entries:
   ```bash
   meho memory list --scope tenant | grep r4-handoff-
   # expect: one entry per interesting event,
   # slug like `r4-handoff-<event_id>`, tag `r4-triage-handoff`
   ```
3. **Local Claude reads the entry.** Open the operator's local
   repo in Claude Code; once `.mcp.json` is in place and the
   session restarts, ask the model:
   > what's interesting on the MEHO backplane right now?

   The session should call `search_memory(scope="tenant",
   query="r4-triage-handoff")` and read back the entry from
   Step 2. The first reply summarises the handoff body.
4. **Audit row exists for the local call.** Confirm the read landed
   under the operator's principal, not the agent's. The MCP
   dispatcher writes one audit row per `tools/call` invocation
   with `op_id` set verbatim to the tool name (per
   [`backend/src/meho_backplane/mcp/handlers.py`](../../backend/src/meho_backplane/mcp/handlers.py)
   line ~248 — `op_id` is the tool `name` field, so the row
   carries `op_id=search_memory` exactly, NOT `memory.search`):
   ```bash
   meho audit query --op-id search_memory --limit 1 --json \
     | jq '.rows[0] | {principal_sub, op_id, occurred_at}'
   ```
   Expect `principal_sub` equal to your Keycloak `sub` — the
   local Claude acted as the operator, not on the operator's
   behalf as a delegated agent.

If step 3 returns nothing, the failure is usually in:

- The token's `tenant_role` claim (back to
  [§ `tools/list` returns an empty list](../../docs/cross-repo/mcp-client-setup.md#toolslist-returns-an-empty-list)).
- The cheap-tier didn't see a sufficiently-interesting event in
  the window. Lower the bar in the system prompt's "interesting"
  list while you're testing, then revert.
- The agent principal lacks the `add_to_memory` grant (Step 4).
  Confirm with `meho audit query --principal agent:r4-alert-triage
  --op-id add_to_memory --result-status denied`.

## Step 6 — Tuning + ops

- **Bump the cron interval** to `*/5 * * * *` if your tenant emits
  high-volume alerts and 15-minute latency on the handoff is too
  loose. Drop to `* * * * *` only on tenants where the per-minute
  cheap-tier round trip is justified — most tenants don't need it,
  and the README framing pins the cadence tradeoff explicitly.
- **Trim the prompt's "interesting" list** to your tenant's
  reality. The shipped prompt's defaults are conservative.
- **Memory TTL is fixed at `P7D`** in the agent's `add_to_memory`
  call (7 days, ISO 8601 duration). After a week the entry ages
  out — the operator should have drained it by then or it wasn't
  a real signal. Drift between this constant and the GUIDE is
  caught by
  [`backend/tests/test_examples_r4_local_claude.py`](../../backend/tests/test_examples_r4_local_claude.py)'s
  schema-validation test.
- **Re-triage of the same event is upsert, not append.** The
  agent prompt encodes this contract explicitly: the slug is
  per-event (`r4-handoff-<event_id>`), and `add_to_memory` is
  last-write-wins on (tenant_id, scope, slug) per
  `MemoryService.remember`. The next 15-minute cron tick on the
  same event replaces the body wholesale; if you want history,
  encode the delta inside the new body before writing.
- **Watch the agent's budget burn.** Per-identity budget enforcement
  ([G11.5-T6 #1080](https://github.com/evoila/meho/issues/1080))
  kills runaway model spend; the cheap-tier should comfortably fit
  in a daily budget cap.

## Why this is the same RBAC as a hosted agent

This is the load-bearing claim from
[Initiative #807](https://github.com/evoila/meho/issues/807)'s
description of R4 — "the *same* identity model + RBAC as a
P1-hosted agent." It holds because:

- The local Claude's token comes from the **same Keycloak realm**
  as the hosted agent's `client_credentials` token. Different `sub`
  (operator vs `agent:r4-alert-triage`) but the same issuer + the
  same audience binding to `MCP_RESOURCE_URI` enforced by
  [`backend/src/meho_backplane/mcp/auth.py`](../../backend/src/meho_backplane/mcp/auth.py).
- Every MCP call from either side runs through the **same
  `verify_mcp_jwt` dependency** before any handler sees it. There
  is no parallel auth boundary for the local client.
- RBAC checks per tool are gated on the principal's role + the
  same per-agent-principal grant table the hosted side reads —
  see G11.2-T3 (per-(principal, op_class, target) durable grants).
- Audit rows for both sides land in the **same `audit_log`
  table** under the same `operator_sub` / `actor_sub` columns.
  The R4 demo and a P1-hosted run leave traces a reviewer can
  query identically.

The deliberate consequence: **no new concepts to learn for the
operator's local session.** Everything you know about how a hosted
agent is wired transfers verbatim to the local Claude path.

## References

- [`README.md`](./README.md) — overview + file index.
- [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
  — full client-side wire-up including troubleshooting matrix.
- [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)
  — deployer-side realm setup (mappers + scopes + audience).
- [`docs/codebase/agent-definition.md`](../../docs/codebase/agent-definition.md)
  — agent definition CRUD semantics.
- [`docs/codebase/scheduler.md`](../../docs/codebase/scheduler.md)
  — cron / one-off / event trigger contract.
- [`docs/codebase/memory.md`](../../docs/codebase/memory.md) —
  memory layer (handoff channel).
- [`docs/codebase/agent-permission-grants.md`](../../docs/codebase/agent-permission-grants.md)
  — per-(principal, op_class, target) grants for the cheap-tier.
- MCP 2025-06-18 spec — <https://modelcontextprotocol.io/specification/2025-06-18>.
- `mcp-remote` shim — <https://github.com/geelen/mcp-remote>.

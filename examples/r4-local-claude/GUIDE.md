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
  `model_tier=fast`, attached to a `* * * * *` cron trigger that
  fires the per-minute triage loop against your tenant's broadcast
  feed.
- A local Claude Code project whose `.mcp.json` points at your
  MEHO instance under the operator's own Keycloak token.
- A working handoff channel: when the hosted agent flags an event
  as interesting, it lands as a memory entry in scope
  `r4-triage-handoff`. The local Claude reads that scope via the
  `search_memory` MCP tool on every "what's interesting?" prompt.

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
   scheduler will impersonate when the cron fires.

## Step 1 — Create the hosted cheap-tier agent definition

The agent definition is in
[`agent.alert-triage.json`](./agent.alert-triage.json). It is a
verbatim `AgentDefinitionCreate` body — pass it through
`meho agent create` via the `@<path>` reader on `--toolset` /
`--output-schema`, but the simplest path is to extract the fields
straight into flag values:

```bash
# Source of truth for the fields is examples/r4-local-claude/agent.alert-triage.json;
# the command below mirrors them. Re-run after editing the JSON.
meho agent create r4-alert-triage \
  --identity-ref "agent:r4-alert-triage" \
  --model-tier fast \
  --turn-budget 8 \
  --system-prompt "$(jq -r .system_prompt examples/r4-local-claude/agent.alert-triage.json)" \
  --toolset "@examples/r4-local-claude/agent.alert-triage.json"
```

> Note on `--toolset @<path>`: the CLI reads the **whole file** and
> expects a JSON object. Passing the agent-definition JSON works
> because the file's `toolset` key is the relevant subset — see the
> implementation in
> [`cli/internal/cmd/agent/create.go`](../../cli/internal/cmd/agent/create.go).
> For a clean separation, extract the `toolset` subobject into its
> own file in your consumer repo.

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
`ScheduledTriggerCreate` payload. Fire-cadence is `* * * * *` (every
minute UTC); change to `*/5 * * * *` (every 5 minutes) if your
broadcast volume is low and a per-minute model round-trip is wasted.

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

Resolve your `agent_definition_id` and create the trigger:

```bash
AGENT_ID=$(meho agent show r4-alert-triage --json | jq -r .id)

# Replace the placeholder UUID in the example payload with the real id.
jq --arg id "$AGENT_ID" '.agent_definition_id = $id' \
  examples/r4-local-claude/scheduler.cron.json > /tmp/r4-trigger.json

meho scheduler create \
  --kind cron \
  --agent-definition "$AGENT_ID" \
  --cron-expr "$(jq -r .cron_expr /tmp/r4-trigger.json)" \
  --timezone   "$(jq -r .timezone   /tmp/r4-trigger.json)" \
  --inputs     "@/tmp/r4-trigger.json"
```

> The `--inputs` flag forwards the whole JSON file's `inputs`
> subobject to the agent run's input string per the scheduler
> contract — see
> [`docs/codebase/scheduler.md`](../../docs/codebase/scheduler.md).

Verify:

```bash
meho scheduler list | grep r4-alert-triage
# expect a row with kind=cron, next_fire_at within a minute
```

Tail one fire and confirm the cheap-tier ran:

```bash
# Wait one cron tick (or `--watch` on the status verb if available),
# then read the most recent run for this agent.
meho agent runs r4-alert-triage --limit 1 --json | jq '{id, status, started_at, ended_at}'
```

A successful run ends in status `succeeded` or `succeeded_no_output`
(the latter is the **happy path here** — "no event was interesting
this minute"). Failures land in `failed` with a structured `error`
field; if you're seeing `failed`, the cheap-tier loop budget
(`turn_budget=8`) may be too low for your broadcast volume — bump
the definition and re-run.

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

Export the token in the operator's shell:

```bash
export MEHO_MCP_TOKEN="$(meho login --print-token)"
```

> Token rotation is per-deployment. The default Keycloak access
> token TTL is short (5–15 minutes); export the variable in a
> rotating helper, or use the Custom Connector / CIMD path which
> manages refresh internally.

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

## Step 4 — Scope grants: read-only vs change-class

The local Claude session inherits the operator's Keycloak token,
which carries the operator's `tenant_role` claim. That role binds
**every** call the session makes via MCP, including the ones the
local model decides to issue without asking:

- `read_only` — read tools work (`meho.status`, `search_memory`,
  `search_knowledge`, `meho.broadcast.recent`); write tools 403.
- `operator` — adds tool-call execution: `meho.agents.run`,
  `meho.connector.*` reads, write tools that the per-(agent_principal,
  op_class, target) **grant table** allows (G11.2-T3).
- `tenant_admin` — adds the admin surface (agent definitions,
  scheduler triggers, broadcast overrides).

**Recommended posture for the operator's local session:**

- Day-to-day triage runs as **`operator`**. The session asks
  "what's interesting?", reads the handoff memory entries, drills
  down into broadcasts and topology, and runs known-good ops.
- For change-class ops (rolling a credential, draining a node), the
  operator either: (a) flips their session token to a
  `tenant_admin` one with a short TTL via a second `meho login`
  invocation, or (b) escalates to a hosted agent run gated by the
  R2 operator-approval flow ([sibling task #1082](https://github.com/evoila/meho/issues/1082)).

> Why scope-grant **the agent**, not the operator: the agent
> principal needs *exactly* the read-broadcast-feed +
> write-handoff-memory grants to do its job — and nothing else.
> G11.2-T3's grant table is where that minimal-scope is bound,
> not the operator's `tenant_role`. The operator's role stays
> coarse on purpose; the agent's grants are fine.

`meho agent-principal` is the verb family:

```bash
# Grant the cheap-tier the minimal scope it needs:
meho agent-principal grant r4-alert-triage --op-class meho.broadcast.recent  --read
meho agent-principal grant r4-alert-triage --op-class add_to_memory          --scope r4-triage-handoff
meho agent-principal grant r4-alert-triage --op-class search_memory          --scope r4-triage-handoff
```

The exact flag names follow the
[G11.2 grant CLI](../../cli/internal/cmd/agent-principal) — read
the help text for the version you have installed; the shape is
stable but flag names may differ across point releases.

## Step 5 — Verify the alerting handoff end-to-end

The verification chain has four steps:

1. **Hosted agent fires.** From Step 2, you already saw a run land
   in `agent runs r4-alert-triage`.
2. **Hosted agent writes a handoff entry** (when an event is
   interesting). Force the path by causing one — e.g. trigger a
   failing op against a write-class target, or `meho broadcast
   announce` an event with `severity=error`. Then:
   ```bash
   meho memory list --scope r4-triage-handoff --limit 5
   # expect: an entry whose slug starts with `event-<some_uuid>`
   ```
3. **Local Claude reads the entry.** Open the operator's local
   repo in Claude Code; once `.mcp.json` is in place and the
   session restarts, ask the model:
   > what's interesting on the MEHO backplane right now?

   The session should call `search_memory(scope="r4-triage-handoff")`
   and read back the entry from Step 2. The first reply summarises
   the handoff body.
4. **Audit row exists for the local call.** Confirm the read landed
   under the operator's principal, not the agent's:
   ```bash
   meho audit query --op-id memory.search --limit 1 --json \
     | jq '{operator_sub, actor_sub, op_id, occurred_at}'
   ```
   Expect `operator_sub == <your keycloak sub>`, `actor_sub == null`
   — the local Claude acted as the operator, not on the operator's
   behalf as a delegated agent.

If step 3 returns nothing, the failure is usually in:

- The token's `tenant_role` claim (back to
  [§ `tools/list` returns an empty list](../../docs/cross-repo/mcp-client-setup.md#toolslist-returns-an-empty-list)).
- The cheap-tier didn't see a sufficiently-interesting event in
  the window. Lower the bar in the system prompt's "interesting"
  list while you're testing, then revert.
- The memory scope strings drifted between the agent prompt and the
  CLI call. Both must read `r4-triage-handoff` verbatim.

## Step 6 — Tuning + ops

- **Bump the cron interval** to `*/5 * * * *` if your tenant emits
  many events and the per-minute decision is wasted.
- **Trim the prompt's "interesting" list** to your tenant's
  reality. The shipped prompt's defaults are conservative.
- **Set a memory TTL** in the agent's `add_to_memory` call (the
  example already uses `604800` = 7 days). After a week the entry
  ages out — the operator should have drained it by then or it
  wasn't a real signal.
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

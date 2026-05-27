<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# R4 — Local Claude as triage (paired with a hosted cheap-tier)

> Reference pattern **R4** under Initiative
> [G11.6 #807](https://github.com/evoila/meho/issues/807). One of four
> runnable examples (R1–R4) that compose the MEHO primitives into
> opinionated, copy-and-go shapes for consumer operators.

## The pattern in one paragraph

An operator's **local Claude Code** session subscribes to alerts and
runs interactive triage **through the same MCP surface** a hosted
agent uses. The token in the operator's `.mcp.json` is minted for the
operator's Keycloak principal, so every call from the local session is
governed by the **same** RBAC the hosted-side agents see — no new
identity model, no parallel auth boundary. Paired alongside is a
24/7 hosted cheap-tier agent (`model_tier=fast`) that watches
broadcast events on a P2 schedule, scores them against a triage
prompt, and **writes the interesting ones to memory under a
well-known scope**. The local Claude reads that scope via the
`search_memory` MCP tool whenever the operator asks "what's
interesting?" — the hosted agent and the local session never exchange
state outside MEHO's own primitives.

## Why "paired with a hosted cheap-tier"

The local Claude is not 24/7. The operator's laptop is off, on a
train, or running a different project. The hosted agent is the
floor — it's the thing that catches the alert at 03:14 and decides
whether to surface it to a human at all. Cheap-tier (`fast`)
matches the workload: the per-alert decision is one round-trip of
"does this need a person?" against a clear triage prompt. A
deep-tier model on this loop would waste budget for the same
verdict.

The local Claude is the **interactive** half. When the operator
sits down, they ask "what landed overnight?" and the session reads
the same memory scope the hosted agent has been writing to. The
operator's session-level token grants the same RBAC the hosted
agent had when it wrote the entries — so every read row is one the
operator's tenant role allows them to see anyway. No cross-tenant
leakage, no second permissions surface.

## What's here

| File | Purpose |
|---|---|
| [`README.md`](./README.md) | This file. The one-paragraph framing. |
| [`GUIDE.md`](./GUIDE.md) | The step-by-step operator recipe: `.mcp.json` wire-up, identity setup, scope grants, pair-up, alerting-flow walkthrough, verification commands. |
| [`agent.alert-triage.json`](./agent.alert-triage.json) | A runnable `AgentDefinitionCreate` payload (model_tier=fast). `meho agent create` consumes the top-level fields (`identity_ref`, `model_tier`, `system_prompt`, `turn_budget`); the `toolset` subobject is split into [`toolset.json`](./toolset.json) for `--toolset @<path>`. |
| [`toolset.json`](./toolset.json) | The split-out `toolset` subobject. `meho agent create --toolset @examples/r4-local-claude/toolset.json` reads this file's JSON as the toolset value (the CLI's `--toolset @<path>` reads the whole file, not just one key — see [`cli/internal/cmd/agent/agent.go`](../../cli/internal/cmd/agent/agent.go) `loadJSONObjectFlag`). |
| [`scheduler.cron.json`](./scheduler.cron.json) | A `ScheduledTriggerCreate` payload (cron, every 15 minutes by default) that fires the triage agent. `meho scheduler create` consumes the top-level fields; the `inputs` subobject is split into [`inputs.json`](./inputs.json) for `--inputs @<path>`. v0.2 ships cron-only because the `kind=event` matcher path is not yet wired ([`events/drain.py`](../../backend/src/meho_backplane/events/drain.py)); the cron loop pulls the latest broadcast events explicitly. |
| [`inputs.json`](./inputs.json) | The split-out `inputs` subobject for `meho scheduler create --inputs @examples/r4-local-claude/inputs.json`. Same split rationale as `toolset.json`. |
| [`mcp.json.example`](./mcp.json.example) | A `.mcp.json` snippet the operator drops into their local Claude Code project root. Covers both the direct HTTP transport and the `mcp-remote` shim path. |

## Composition only — no new MEHO surface

This pattern is **not** new MEHO API. The hosted agent uses
`meho agent create` + `meho scheduler create` (G11.1 / G11.3).
The handoff channel is `add_to_memory` / `search_memory`
([G5](https://github.com/evoila/meho/issues/204) memory layer,
exposed as MCP tools by [`backend/src/meho_backplane/mcp/tools/memory.py`](../../backend/src/meho_backplane/mcp/tools/memory.py)).
The local Claude connects via the standard MCP Streamable HTTP
transport documented in
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md).
The same surface is governed identically by `verify_mcp_jwt`
([`backend/src/meho_backplane/mcp/auth.py`](../../backend/src/meho_backplane/mcp/auth.py))
whether the caller is the hosted agent or the local Claude — the
audience binding + RFC 8693 delegation
([`backend/src/meho_backplane/auth/delegation.py`](../../backend/src/meho_backplane/auth/delegation.py))
do the rest.

The deliverable is **the composition**, exercised in CI so the
example stays current with the primitives.

## Pre-requisites — what must be wired before this example runs

- A working MEHO instance with the MCP surface reachable
  (`MCP_RESOURCE_URI` resolved per
  [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)).
- A Keycloak realm with the audience mapper for `MCP_RESOURCE_URI`
  and the consolidated auth onramp from
  [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
- An agent principal registered for the cheap-tier agent
  (G11.2-T1 #815). `meho agent-principal register` is the verb.
- An operator account with role `operator` or `tenant_admin`
  (otherwise the local Claude's `tools/list` lands empty —
  see [§ `tools/list` returns an empty list](../../docs/cross-repo/mcp-client-setup.md#toolslist-returns-an-empty-list)).

## Verifying the example after install

[`GUIDE.md`](./GUIDE.md) § Verification walks through the four-step
verification chain. The CI smoke
[`backend/tests/test_examples_r4_local_claude.py`](../../backend/tests/test_examples_r4_local_claude.py)
runs the schema checks unattended on every PR.

## References

- Initiative [G11.6 #807](https://github.com/evoila/meho/issues/807),
  pattern R4.
- Task [G11.6-T4 #1083](https://github.com/evoila/meho/issues/1083).
- MCP surface — [`backend/src/meho_backplane/mcp/`](../../backend/src/meho_backplane/mcp/).
- MCP client setup —
  [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md).
- Auth onramp (deployer + MCP wire-up) —
  [`deploy/values-examples/README.md`](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
- RFC 8693 delegation in MEHO —
  [`backend/src/meho_backplane/auth/delegation.py`](../../backend/src/meho_backplane/auth/delegation.py).
- Agent definitions —
  [`docs/codebase/agent-definition.md`](../../docs/codebase/agent-definition.md).
- Scheduler — [`docs/codebase/scheduler.md`](../../docs/codebase/scheduler.md).
- Memory layer (handoff channel) —
  [`docs/codebase/memory.md`](../../docs/codebase/memory.md).

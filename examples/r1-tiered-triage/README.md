<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# R1 — Tiered-triage reference sample (cheap + deep, closed-loop)

> Reference pattern **R1** under Initiative
> [G11.6 #807](https://github.com/evoila/meho/issues/807) — the
> flagship and the Goal's top-line success criterion
> ([G11 #800](https://github.com/evoila/meho/issues/800)). One of
> four runnable examples (R1–R4) that compose the MEHO primitives
> into opinionated, copy-and-go shapes for consumer operators.

## The pattern in one paragraph

A **cheap-tier classifier** (`model_tier=fast`) runs on a
[`*/15 * * * *`](./scheduler.cron.json) cron schedule. On every
firing it reads the latest broadcast events and the tenant's
accumulated **triage policy** (memory scope `tenant`, slugs
prefixed `r1-policy-`). For each event it decides
**skip / escalate**; for each escalation it calls
[`invoke_agent`](../../backend/src/meho_backplane/agent/invoke.py)
against the **deep-tier investigator** (`model_tier=deep`). The
deep tier investigates, returns a structured `PolicyDecision`, and
the [harness](./workflow.py) writes the decision back to memory as
`r1-policy-<alert_class>`. The next firing of the cheap tier reads
the new policy entry and short-circuits re-triage of the same
class of event — that is the **closed loop**, and that is what
distinguishes "noisy continuous-classification harness" from
"agentic system that gets cheaper over time".

## Why this is the flagship reference

The parent [Goal G11 #800](https://github.com/evoila/meho/issues/800)
states its done-when as *"the tiered-triage harness can be built
using only MEHO APIs, with no custom backend in the consuming
repo."* R1 is the literal instance of that harness: runnable
sample agent definitions, schedule, identity setup, cost limits,
and the closed-loop policy-memory write-back, exercised in CI so
it cannot rot. The other three patterns (R2 approval gate, R3 kb
write-back, R4 local-Claude triage) compose on top of the same
primitives; R1 is the one that proves the primitives compose at
all.

## Composition only — no new MEHO surface

This pattern is **not** new MEHO API. The cheap + deep agents use
`meho agent create` ([G11.1 #802](https://github.com/evoila/meho/issues/802));
the schedule uses `meho scheduler create`
([G11.3 #804](https://github.com/evoila/meho/issues/804)); the
escalation rides the existing `invoke_agent` meta-tool
([G11.1-T5 #812](https://github.com/evoila/meho/issues/812)); the
identity model is Keycloak agent principals
([G11.2 #803](https://github.com/evoila/meho/issues/803)); the
cost gate is the per-identity budget table
([G11.5-T5 #1079](https://github.com/evoila/meho/issues/1079))
plus the pre-execution degrade/refuse policy
([G11.5-T6 #1080](https://github.com/evoila/meho/issues/1080));
the closed-loop channel is the
[memory service](../../backend/src/meho_backplane/memory/service.py)
(G5). The deliverable is **the composition**, exercised in CI so
the example stays current with the primitives.

## What's here

| File | Purpose |
|---|---|
| [`README.md`](./README.md) | This file. The one-paragraph framing. |
| [`GUIDE.md`](./GUIDE.md) | The step-by-step operator recipe: principal registration, agent creation, scheduler wire-up, permission grants, budget seeding, end-to-end verification. |
| [`agent.cheap-tier-classifier.json`](./agent.cheap-tier-classifier.json) | The cheap-tier `AgentDefinitionCreate` payload (`model_tier=fast`, `turn_budget=12`, escalate-only toolset). `meho agent create` consumes it. |
| [`agent.deep-tier-investigator.json`](./agent.deep-tier-investigator.json) | The deep-tier `AgentDefinitionCreate` payload (`model_tier=deep`, `turn_budget=25`, structured `PolicyDecision` output). |
| [`scheduler.cron.json`](./scheduler.cron.json) | The `ScheduledTriggerCreate` payload (`*/15 * * * *`) that fires the cheap tier. `meho scheduler create` consumes it. |
| [`permissions.json`](./permissions.json) | `AgentPermission` grants for both principals. Cheap tier: read broadcast + escalate to the deep tier (nothing else). Deep tier: read-only across discovery connectors; any write op routes through the approval gate. |
| [`identity_budget_seed.py`](./identity_budget_seed.py) | Operator-run script that seeds the daily `identity_budget` row for each agent principal via `set_limits`. |
| [`workflow.py`](./workflow.py) | The runnable closed-loop harness: loads known policy, runs the cheap tier, intercepts escalations via the `invoke_agent` recorder/finalizer hooks, persists each `PolicyDecision` to memory under `r1-policy-<alert_class>`. |
| [`../../backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py) | The CI exercise. Validates the runnable JSON against the live schemas, walks markdown links for rot, and drives the harness end-to-end against an in-memory SQLite + deterministic FunctionModel for the loop. |

## Pre-requisites — what must be wired before this example runs

- A working MEHO backplane on v0.2 or later (agent runtime, scheduler,
  agent identity, tiered model resolver, per-identity budgets all
  shipped).
- A Keycloak realm wired per the
  [auth onramp recipe](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
- An agent principal registered for **each** of the two agents
  ([G11.2-T1 #815](https://github.com/evoila/meho/issues/815)).
  `meho agent-principal register` is the verb. The cheap and deep
  tiers each get their own Keycloak `client_credentials` client and
  their own `sub` claim.
- A tenant-admin role for setting up grants + seeding budgets.

## Verifying the example after install

[`GUIDE.md`](./GUIDE.md) §
[Verification](./GUIDE.md#step-6--verify-the-closed-loop) walks
through the four-step verification chain: stage an event in the
broadcast feed, trigger the cheap tier manually, confirm the
deep-tier run record landed and the `r1-policy-*` memory entry
appeared, re-trigger the cheap tier and confirm it short-circuits
on the new policy.

The CI smoke
[`backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py)
runs the schema validation + link resolution + the in-process
closed-loop drive on every PR.

## References

- Initiative [G11.6 #807](https://github.com/evoila/meho/issues/807),
  pattern R1.
- Task [G11.6-T1 #1084](https://github.com/evoila/meho/issues/1084).
- Sibling patterns (in flight under the same Initiative): R2
  approval gate (PR [#1243](https://github.com/evoila/meho/pull/1243)),
  R3 kb write-back (PR [#1245](https://github.com/evoila/meho/pull/1245)),
  R4 local Claude (PR [#1244](https://github.com/evoila/meho/pull/1244)).
  Cross-pattern links land after the sibling PRs merge.
- Agent runtime — [`docs/codebase/agent-runtime.md`](../../docs/codebase/agent-runtime.md).
- Agent-invokes-agent (the escalate path) —
  [`backend/src/meho_backplane/agent/invoke.py`](../../backend/src/meho_backplane/agent/invoke.py).
- Scheduler — [`docs/codebase/scheduler.md`](../../docs/codebase/scheduler.md).
- Identity + RBAC —
  [`docs/codebase/agent-permission-model.md`](../../docs/codebase/agent-permission-model.md).
- Per-identity budgets —
  [`docs/codebase/identity-budget.md`](../../docs/codebase/identity-budget.md).
- Memory layer (the closed-loop channel) —
  [`docs/codebase/memory.md`](../../docs/codebase/memory.md).
- Architecture deep-dive —
  [`docs/codebase/examples-r1-tiered-triage.md`](../../docs/codebase/examples-r1-tiered-triage.md).

# examples/r1-tiered-triage — the tiered-triage reference pattern

## Overview

R1 is one of four reference patterns ([Initiative G11.6 #807](https://github.com/evoila/meho/issues/807))
shipped under `examples/`. Its purpose is to be the **flagship**
composition of G11's primitives: cheap-tier classifier on a P2
schedule, escalating to a deep-tier investigator via
agent-invokes-agent, with the deep tier's verdicts persisted back
to the memory layer (G5) as the policy the cheap tier reads on
every firing — closing the triage loop so the system gets cheaper
to run over time as the policy accumulates.

R1 is **composition only**. It introduces no new MEHO surface; it
exists to prove the primitives compose into the consumer's
top-line use case ([Goal G11 #800](https://github.com/evoila/meho/issues/800)'s
done-when: *"the tiered-triage harness can be built using only
MEHO APIs, with no custom backend in the consuming repo"*).

## Key types

| Type | Where | Role |
|---|---|---|
| `BroadcastEvent` | `examples/r1-tiered-triage/workflow.py` | One classifiable event; the input to the cheap tier |
| `PolicyDecision` | `examples/r1-tiered-triage/workflow.py` | Structured output of one deep-tier investigation |
| `PolicyWriteBack` | `examples/r1-tiered-triage/workflow.py` | One harness-issued memory write record |
| `TriageRunResult` | `examples/r1-tiered-triage/workflow.py` | The structured return of one `run_closed_loop` call |
| `r1-cheap-tier-classifier` | `agent.cheap-tier-classifier.json` | The `AgentDefinitionCreate` payload (`fast` tier) |
| `r1-deep-tier-investigator` | `agent.deep-tier-investigator.json` | The `AgentDefinitionCreate` payload (`deep` tier, structured output) |
| Memory scope `tenant`, slug prefix `r1-policy-` | the closed-loop channel | Where the deep tier's verdicts persist |

## Control flow

```
                  ┌──────────────────────────────────┐
scheduler tick ──►│ fire(r1-cheap-tier-classifier)   │
(*/15 * * * *)    └────────────────┬─────────────────┘
                                   │
                                   ▼
                   ┌───────────────────────────────────┐
                   │ harness.run_closed_loop(...)      │
                   │                                   │
                   │  1. load_known_policy()           │
                   │       → MemoryService.list_       │
                   │         memories(scope=tenant,    │
                   │         slug_pattern=r1-policy-)  │
                   │                                   │
                   │  2. build_cheap_tier_input()      │
                   │       ## Known policy  + entries  │
                   │       ## Recent events + JSON     │
                   │                                   │
                   │  3. PydanticAgentRun.start(cheap) │
                   └───────────────┬───────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │ cheap-tier loop                      │
                │                                      │
                │  for event in events:                │
                │    if covered_by_policy(event):      │
                │      skip                            │
                │    elif interesting(event):          │
                │      invoke_agent("r1-deep-...", b)  │
                │      └──┐                            │
                └─────────┼────────────────────────────┘
                          │
                          ▼ (each escalation)
                ┌──────────────────────────────────────┐
                │ make_invoke_agent_tool() body:       │
                │  - depth check                       │
                │  - resolve child (returns deep_def)  │
                │  - recorder()  ← harness hook        │
                │      → append to escalations_observed│
                │  - child_runner(deep_def, ...)       │
                │      → deep-tier loop runs           │
                │  - finalizer(output)  ← harness hook │
                │      → if PolicyDecision:            │
                │           persist_policy_decision()  │
                │             → MemoryService.remember │
                │             (scope=tenant,           │
                │              slug=r1-policy-<class>) │
                └────────────────────┬─────────────────┘
                                     │
                                     ▼
                  ┌────────────────────────────────────┐
                  │ cheap-tier returns its final answer│
                  │ (one-line-per-event terse summary) │
                  └────────────────────────────────────┘

next tick (15 min later): step 1 reads the newly-written
                          r1-policy-<class> entry; the cheap
                          tier short-circuits the same event class
                          on this firing.
```

## The closed-loop contract

Two directional flows compose into one loop:

- **Cheap reads policy memory.** Every firing, before the cheap
  tier sees the broadcast events, the harness calls
  `load_known_policy(operator)` which lists every memory entry in
  scope `tenant` whose slug starts with `r1-policy-`. The entries
  are flattened into a `## Known policy` section in the cheap
  tier's loop input. The cheap-tier prompt names the section by
  heading and treats every entry as an authoritative override.
- **Deep writes policy memory.** Every successful deep-tier
  investigation returns a `PolicyDecision`. The harness's
  `finalizer` hook (plugged into `make_invoke_agent_tool`'s
  `finalizer` slot) calls `persist_policy_decision`, which writes
  the `PolicyDecision` to scope `tenant`, slug `r1-policy-<alert_class>`.
  `MemoryService.remember` upserts on the `(scope, slug)` key, so
  re-running an investigation on the same alert class overwrites
  the prior verdict rather than spawning a duplicate.

The loop is **bidirectional**: the cheap tier consumes policy
entries the deep tier produced; future deep-tier verdicts overwrite
or refine them. There is no separate audit / promotion step — the
memory entry IS the policy, and the cheap tier reads it directly
on every firing.

## Why the harness exists (and why R3 has one too)

The MCP memory tools (`add_to_memory` / `search_memory`,
`backend/src/meho_backplane/mcp/tools/memory.py`) are not
registered as in-process agent tools in v0.2 — the loop's tool
catalog (`META_TOOL_NAMES` in `backend/src/meho_backplane/agent/toolset.py`)
contains only `list_operation_groups` + `search_operations` +
`call_operation` (plus `invoke_agent` when composition is wired).
An agent definition that names `add_to_memory` in its toolset spec
gets a logged warning and the tool is not registered; the model
cannot call it.

Both R1 and R3 (`examples/kb_writeback`) work around this by
doing the memory/kb write **in the harness**, not in the loop:

- R3: the investigation agent returns a structured `Finding`; the
  harness calls `KbService.create_entry` after the loop returns.
- R1: the deep-tier agent returns a structured `PolicyDecision`;
  the harness's `invoke_agent` finalizer hook calls
  `MemoryService.remember` after the child loop returns.

The pattern is the **same**: the agent emits a typed value, the
harness persists it. Pulling the write out of the loop keeps the
prompt narrow ("produce a decision") and the persistence boundary
auditable in plain Python. A future task that exposes the MCP
memory tools to the in-process loop would let the deep agent issue
its own `add_to_memory` call, at which point both R1 and R3 could
collapse their harnesses to a pure deploy of the agent definitions.
The composition shape would not change.

## Identity inheritance across the escalate edge

`make_invoke_agent_tool`'s tool body line `operator = ctx.deps` is
unconditional: the child agent (the deep tier) runs under the
parent's identity (the cheap tier's `Operator`). This means:

- Cheap tier's tenant + role + sub are what the deep tier sees as
  its `Operator`.
- The deep tier's own `AgentPermission` rows (keyed on the deep
  tier's principal `sub` from `agent:r1-deep-tier-investigator`)
  layer on top of the inherited operator's role.
- The audit row for the deep tier's child run records both
  identities: `act_sub` = the deep tier's principal, `parent_run_id`
  = the cheap tier's run id.

Lineage is therefore reconstructable from the audit log: a
forensic walker follows `parent_run_id` up to the cheap tier's
scheduled-trigger row and out to the broadcast event that fired
it; it follows `act_sub` to each tier's principal and the grants
they hold.

## Cost interaction

Two budgets fire on every closed-loop tick:

- **Cheap-tier budget.** The cheap tier's principal's daily
  `identity_budget` row. Refused at `cost_consumed >= cost_limit`
  (no model call); degraded at `cost_consumed >= 0.8 * cost_limit`
  (no-op for the cheap tier — its requested tier is already
  `fast`).
- **Deep-tier budget.** The deep tier's principal's daily row.
  Same enforcement, but degradation is meaningful (the resolver
  routes `deep` -> `fast` at the threshold; the prompt still asks
  for a `PolicyDecision`, but the cheaper model produces it). A
  hit on `cost_limit` raises `BudgetExceededError`; the cheap
  tier's `invoke_agent` call surfaces a `ModelRetry` ("child agent
  failed: budget_exceeded"). The cheap tier can then decide
  whether to skip the event, defer it, or surface it to the
  operator as an unprocessable escalation.

The seed values in `identity_budget_seed.py` are deliberately
small (`$2/day` cheap, `$10/day` deep) so a real-world consumer
copying this example out has to think about the numbers rather
than land on a multi-thousand-dollar cap by accident.

## Dependencies

- `meho_backplane.agent.run.AgentDefinition` /
  `meho_backplane.agent.run.PydanticAgentRun` —
  [`backend/src/meho_backplane/agent/run.py`](../../backend/src/meho_backplane/agent/run.py).
- `meho_backplane.agent.invoke.make_invoke_agent_tool` —
  [`backend/src/meho_backplane/agent/invoke.py`](../../backend/src/meho_backplane/agent/invoke.py)
  (the recorder + finalizer hooks the harness plugs into).
- `meho_backplane.memory.service.MemoryService` —
  [`backend/src/meho_backplane/memory/service.py`](../../backend/src/meho_backplane/memory/service.py)
  (the closed-loop channel).
- `meho_backplane.scheduler` — the cron-triggered firing surface
  (see [`docs/codebase/scheduler.md`](./scheduler.md)).
- `meho_backplane.operations.identity_budget` /
  `meho_backplane.operations.budget_enforcement` — the daily
  budget rows + the pre-execution gate (see
  [`docs/codebase/identity-budget.md`](./identity-budget.md)).
- `meho_backplane.agents.schemas.AgentDefinitionCreate` /
  `meho_backplane.scheduler.schemas.ScheduledTriggerCreate` — the
  wire shapes the JSON payloads validate against; drift here is
  caught by the schema-validation tests in
  [`backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py).

## Known issues

- The MCP memory tools are not in the in-process agent tool
  catalog; the deep tier cannot write `r1-policy-*` entries
  directly from its loop. The harness's `finalizer` hook persists
  on the agent's behalf. Once a future task exposes the MCP
  memory tools to the loop, the harness can be deleted in favour
  of an in-prompt `add_to_memory` call.
- The cheap tier's "rate baseline crossed by 3x" rule is documented
  in the system prompt as a signal source but requires the event
  payload to carry `rate_baseline_x`. The broadcast feed does not
  yet stamp that field; an in-flight enhancement to the broadcast
  publisher would close that gap.
- The harness builds a fresh `PydanticAgentRun` per
  `run_closed_loop` call rather than reusing the caller's
  runtime. A consumer wanting per-tick metrics on the same
  runtime instance (e.g. shared `run_child` interceptor) needs to
  subclass `PydanticAgentRun` and pass the subclass to the
  harness.

## References

- Initiative [G11.6 #807](https://github.com/evoila/meho/issues/807).
- Task [G11.6-T1 #1084](https://github.com/evoila/meho/issues/1084).
- Goal [G11 #800](https://github.com/evoila/meho/issues/800) (the
  parent done-when).
- Agent runtime — [`docs/codebase/agent-runtime.md`](./agent-runtime.md).
- Memory layer — [`docs/codebase/memory.md`](./memory.md).
- Per-identity budget — [`docs/codebase/identity-budget.md`](./identity-budget.md).
- Scheduler — [`docs/codebase/scheduler.md`](./scheduler.md).
- Sibling reference pattern docs (when they land):
  R2 approval gate (PR [#1243](https://github.com/evoila/meho/pull/1243)),
  R3 kb write-back (PR [#1245](https://github.com/evoila/meho/pull/1245)),
  R4 local-Claude (PR [#1244](https://github.com/evoila/meho/pull/1244)).

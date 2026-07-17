# Agent runtime (G11)

## Overview

The agent runtime makes MEHO an in-process agent host: an LLM agent's
tool-use loop runs inside the backplane process and every tool call is
routed through the existing dispatch path (`call_operation` → `dispatch`),
so the agent is governed by the same identity, RBAC, audit, and
sanitization machinery that governs human operators.

The runtime is built behind a narrow seam, `AgentRun`, so the third-party
loop framework (Pydantic AI) stays swappable and the invocation surface,
run-handle store, and audit wiring remain MEHO's. Nothing outside
`meho_backplane.agent` imports `pydantic_ai`.

This document describes the G11.1-T1 foundation (the seam + one bounded
loop), the G11.1-T3 toolset resolution layered on it, the G11.1-T4
public invocation surface (sync + async handle/poll/SSE on REST + MCP +
CLI) built on top, and the G11.1-T5 agent-invokes-agent composition.
Other G11.1 tasks: agent-definition persistence (T2) and durable run
records (T6 — whose `agent_run` row the T4 surface reads/writes).

## Key types

All public types are re-exported from `meho_backplane.agent`
(`agent/__init__.py`); they are defined in `meho_backplane/agent/run.py`.

- **`AgentDefinition`** — frozen Pydantic model holding the static shape of
  one run: `name`, `system_prompt`, `request_limit` (the turn budget),
  optional `model` id override, optional `output_type` (a Pydantic model
  class for structured output), and optional `toolset` (the spec
  `resolve_agent_tools` intersects with the run's identity; `None` selects
  the default two-meta-tool surface). T2 materialises persisted rows into
  this shape.
- **`resolve_agent_tools` / `MetaToolSpec` / `META_TOOL_NAMES`** (in
  `agent/toolset.py`) — the toolset resolver + handler-to-agent-tool adapter.
  `resolve_agent_tools(toolset, operator)` returns the `pydantic_ai.Tool`
  list to register: (spec allow-list) ∩ (meta-tools the operator's role
  admits). See "Tools" below.
- **`AgentRun`** — the runtime-checkable Protocol every consumer depends
  on: `start(definition, operator, inputs) -> AgentRunHandle`,
  `poll(handle) -> AgentRunStatus`, `result(handle) -> AgentRunResult`,
  `stream(handle) -> AsyncIterator[str]`.
- **`PydanticAgentRun`** — the Pydantic AI-backed implementation of the
  Protocol. Holds an injected `ModelFactory`; otherwise stateless, so one
  instance is safe to share across runs.
- **`AgentRunHandle`** — a reference to one in-flight or finished run.
  Wraps the backing `asyncio.Task`, which is the single source of truth for
  lifecycle state (`poll` derives the status from it).
- **`AgentRunResult`** — terminal outcome: the loop's `output` (a validated
  `output_type` instance when structured, else free text), plus
  `request_count` and `tool_call_count` lifted from the framework's usage
  accounting.
- **`AgentRunStatus`** — closed enum: `RUNNING` / `SUCCEEDED` / `FAILED`.
- **`AgentRunError`** — domain exception raised when a run cannot start or
  its result is unavailable (turn budget exhausted, a tool raised, the
  model errored). The seam's single failure type, so callers catch one
  thing regardless of which framework exception fired underneath.
- **`ModelFactory`** — `Callable[[], Model]`. A factory (not a singleton)
  so the model can be lazy-built after settings change and tests can inject
  a deterministic `FunctionModel`. `default_model_factory` builds an
  Anthropic model from settings.

## Control flow

1. A caller constructs an `AgentDefinition` and a `PydanticAgentRun`
   (injecting a `ModelFactory`; the default builds a real Anthropic model).
2. `start(definition, operator, inputs)` builds a framework `Agent` from the
   definition, resolves its tools (`resolve_agent_tools` when the definition
   carries a `toolset`, else the default two meta-tools), and launches the
   bounded loop as an `asyncio.Task`. It returns immediately with an
   `AgentRunHandle`.
3. The loop runs: the model emits tool calls, each tool dispatches through
   `call_operation` / `list_operation_groups` under the injected operator,
   tool results return to the model, and the loop repeats until the model
   produces a final answer or the turn budget trips.
4. The caller `poll`s for status, `await result(handle)` to block for the
   terminal `AgentRunResult`, or consumes `stream(handle)`.

The **turn budget** maps onto `UsageLimits(request_limit=...)`. When the
loop would exceed it, the framework raises `UsageLimitExceeded`, which the
seam converts to `AgentRunError("turn budget exhausted: ...")`.

The **operator** travels as the framework dependency (`deps_type=Operator`);
inside each tool the operator is read from `ctx.deps` and passed to the
`(operator, arguments) -> dict` meta-tool handler, so dispatch, RBAC, and
audit see the real principal.

## Tools

The loop's tools are MEHO's own meta-tools, adapted from their
`(operator, arguments) -> dict` handler shape onto the framework's
`RunContext`-first tool signature. The handler *is* the dispatch path REST +
MCP use; the agent gets no vendor-specific surface (CLAUDE.md postulate 5 —
the agent never sees `vsphere.vm.list`; it reaches every connector op
*through* `call_operation`).

### Toolset resolution (T3 #810)

Which meta-tools register is decided by `resolve_agent_tools` in
`meho_backplane/agent/toolset.py`, given the definition's `toolset` spec and
the run's operator. The registered set is the **intersection** of:

- **toolset spec** — the definition author's allow-list (`AgentDefinition.toolset`).
- **identity permissions** — what the run's identity may call. The G11.2
  per-op permission *model* is not built yet; the permission an identity
  carries today is its `TenantRole`, gated by `role_at_least` exactly as the
  MCP and REST surfaces gate it. Each meta-tool declares a role floor; an
  identity below the floor does not get the tool. This Task *consumes* that
  gate, it does not invent a new one.

A meta-tool failing either side is **not registered** — it is absent from the
agent's tool surface, so the model cannot attempt it (least-privilege:
the safest tool is one that does not exist on the surface).

The `PydanticAgentRun` seam routes through the resolver when the definition
carries a `toolset` (passing the resolved `Tool` objects via the framework's
`tools=` constructor argument). A definition with `toolset is None` falls
back to `_register_default_meta_tools` — the original two hand-wired
meta-tools (`call_operation` + `list_operation_groups`) — so a definition
constructed without a toolset still runs.

### Toolset spec shape

JSON-shaped (so it can grow without a migration):

```json
{
  "meta_tools": ["list_operation_groups", "search_operations", "call_operation"],
  "connectors": ["vmware-rest-9.0", "vault-1.x"]
}
```

- `meta_tools` — allow-list of meta-tool names. Omitted / `null` means "all
  meta-tools the role admits"; `[]` registers nothing. The catalog of known
  meta-tools is `META_TOOL_NAMES`; an unknown name in the list is ignored
  with a warning (forward-compat).
- `connectors` — optional allow-list of `connector_id` values the agent may
  reach through `call_operation`. Omitted / `null` means "no connector-level
  restriction beyond what dispatch already enforces" (the tenant boundary +
  per-op RBAC still apply); `[]` forbids every connector. A `call_operation`
  to a connector outside the list is rejected with a `ModelRetry` — a
  structured, agent-reasonable error the model can recover from, not a crash.
  This keeps the "connector-ops ∩ identity permissions" intersection honest
  without putting vendor identifiers in tool names (postulate 5). Per-op RBAC
  + tenant scoping + audit + sanitization still fire unchanged inside
  `dispatch`; the agent layer adds connector-level scoping on top.

The adapter (`MetaToolSpec` catalog) is the agent front-end's source of truth
for *its* tool surface, parallel to the MCP front-end's `register_mcp_tool`
registrations — both adapt the same `meta_tools` handlers; neither wraps the
other.

### Broadcast coordination tools (#2548)

Beyond the connector-execution tools, the catalog carries three broadcast
coordination tools so a hosted run is a first-class reader and writer on the
tenant's shared feed (`meho:feed:{tenant_id}`) rather than a mute
participant:

- `broadcast_announce` — publish intent / progress / completion.
- `broadcast_recent` — read recent feed events (peer announcements + audit
  events).
- `broadcast_watch` — a single bounded long-poll (≤30s) for new events; no
  background subscription.

These do **not** re-implement the feed. `_build_broadcast_meta_tools` in
`toolset.py` resolves the `(inputSchema, handler)` pairs the MCP surface
already registered for `meho.broadcast.{announce,recent,watch}` (via the
registry's `get_tool`) and reuses the handlers verbatim, so the agent's wire
shape — the #2544 structured claims, the #2545 actor/work_ref lineage, the
#2546 announce rate limit, and the untrusted-prose envelope
(`dump_event_wire`) on reads — is identical to every other surface's. The
read tools reuse the MCP `inputSchema` unchanged; `broadcast_announce` reuses
it minus `run_id` / `work_ref`, which the wrapper stamps from the ambient run
context (`current_agent_run_id_var` + `work_ref_var`, the same ContextVars the
audit writers and `approval_wait` read) so announcements auto-group under the
run without the model self-reporting a spoofable id. Handler-side errors
(`McpInvalidParamsError`, the announce `McpRateLimitedError`) are re-raised as
`ModelRetry` so a bad argument or a rate-limit hands feedback to the model
instead of aborting the run.

All three carry an `OPERATOR` floor (matching the MCP tools' `required_role`)
and register only when a definition's `toolset` admits them — the same
`spec ∩ identity-permissions` intersection as every other meta-tool. A
definition with `toolset is None` uses the legacy `_register_default_meta_tools`
fallback (the original two tools) and does not gain the broadcast surface; a
definition wanting coordination lists them (or omits `meta_tools` to take the
whole catalog).

## Invocation surface (T4 #811)

The public way to *run* a defined agent: synchronous block-and-return for
short runs and asynchronous handle + poll + SSE for long ones, on all three
fronts (REST + MCP + CLI). The surface is built on the T1 seam and the T6
`agent_run` row.

### Orchestration — `agent/invocation.py`

`AgentInvoker` (singleton via `get_agent_invoker()`; tests inject a
deterministic seam via `reset_agent_invoker_for_testing`) ties the seam, the
persisted definition (T2 `AgentDefinitionService`), and the durable run row
(T6 `operations/agent_run.py`) together:

- **`run(operator, name, inputs, async_mode=False) -> AgentRunOutcome`** —
  resolves the named *enabled* definition for the operator's tenant
  (`AgentNotFoundError` / `AgentDisabledError` otherwise), creates the
  durable `agent_run` row (`create_run` → `start_run`, committed before the
  loop starts so the run is pollable immediately), and launches the loop as
  a background `asyncio.Task` anchored in the invoker's **run store**. Sync
  awaits the run up to `agent_sync_timeout_seconds`; on timeout the loop
  keeps running and the call returns the still-running handle with
  `converted_to_async=True`. Async returns the handle immediately.
- **`poll(operator, run_id) -> AgentRunStatusView`** — reads the durable
  `agent_run` row (the source of truth), so it works after the request that
  started the run has returned, and even after the in-memory task is gone
  (worker restart). Cross-tenant / unknown ids raise `AgentRunNotFoundError`.
- **`stream_events(operator, name, inputs)`** — drives the seam's
  `stream_events` inline (one connection = one run's lifetime), yielding
  `(run_id, AgentRunEvent)` and recording the terminal outcome on the run
  row so a streamed run is still poll-able afterward.

**Why a run store.** The seam's `AgentRunHandle` wraps an in-memory
`asyncio.Task`. A FastAPI request's task tree is torn down when the request
returns, so a fire-and-forget async run would be cancelled the instant the
call returns. The invoker keeps background tasks on the application event
loop, anchored by a strong reference (asyncio only weakly references bare
tasks), so they outlive the creating request; the durable `agent_run` row is
the source of truth for status/output, so poll survives a worker restart.

### Seam event stream — `AgentRun.stream_events`

`AgentRun.stream_events(definition, operator, inputs, run_id)` is the richer
stream T1's `stream` deferred to T4. It drives the framework node graph
(`Agent.iter`) and yields `AgentRunEvent`s — `turn` / `tool_call` /
`tool_result` / `final` / `error` — without leaking framework types across
the seam (`AgentRunEvent` is a plain value object; `AgentRunEventKind` a
closed enum). A tripped turn budget surfaces as a terminal `error` event,
not a raised exception, so an SSE consumer always sees a terminal frame.

### Fronts

- **REST** (`api/v1/agent_runs.py`): `POST /api/v1/agents/{name}/run`
  (200 with the result for a terminal sync run; 202 with a handle for async
  / converted-to-async), `GET /api/v1/agents/runs/{handle}` (poll),
  `POST /api/v1/agents/runs/{handle}/cancel` (cancel — #1828: transitions a
  non-terminal run to `cancelled` via the shared `cancel_run` service path,
  returns the updated `AgentRunSummaryResponse`; 404 for an unknown /
  cross-tenant handle, 409 for an already-terminal run), and
  `POST /api/v1/agents/{name}/run/events` (SSE — `text/event-stream`, the
  G6 broadcast-feed transport shape). The poll/cancel/events sub-paths are
  two-or-more segments deep so they never collide with the one-segment
  `/{name}` definition-CRUD routes.
- **MCP** (`mcp/tools/agent_runs.py`): `meho.agents.run` (sync/async via an
  `async` arg) + `meho.agents.run_status` (poll). SSE is REST-only; an MCP
  caller polls. Cancel is REST/CLI-only (no MCP verb). Both drive the same
  `AgentInvoker` singleton, so a run started over MCP is poll-able over REST
  and vice versa.
- **CLI** (`cli/internal/cmd/agent/run.go`, `run_status.go`,
  `run_cancel.go`, `run_events.go`): `meho agent run <name> --input …
  [--async]`, `meho agent run-status <handle>`, `meho agent run-cancel
  <handle>` (#1828), `meho agent run-events <name> --input …`.

All fronts require the `operator` role and are tenant-scoped via the JWT.

### Why the SSE events route is a POST that starts a fresh run

The issue's literal shape was `GET …/runs/{handle}/events`. SSE needs the
run *input*, and the seam streams a fresh run per connection (the same
fresh-stream-per-connection shape the G6 broadcast feed uses), so the route
is `POST /agents/{name}/run/events` — the input arrives in the body and one
connection drives one run's lifetime. The run is recorded on a durable
`agent_run` row (every frame's `data:` carries the `run_id`), so the
poll-after-stream contract the issue intends still holds.

### Run-start guards — refuse a doomed loop before the model call

Two typed pre-flight guards in `agent/invocation.py` fail a run *closed*
before the loop (and any model call) starts. Both follow the same shape:
the durable `agent_run` row is created first (so the refusal is observable
in the runs table and via `poll`, like any other terminal run), then the
row is finalised `failed` with a machine-classifiable error prefix — never
a raised exception across the boundary, and never a launched loop.

- **Scheduled no-input (#1505,** `SCHEDULED_RUN_NO_INPUT_CLASS`**)** — a
  scheduled trigger fired with an empty / whitespace-only prompt would
  reach the provider as an empty `messages` array and come back as an
  opaque 400; instead the row fails typed with a
  `scheduled_run_no_input:`-prefixed error. Scheduled path only.
- **Unexecutable runbook reference (#2077,**
  `UNEXECUTABLE_RUNBOOK_CLASS`**)** — a prompt (system prompt *or* run
  inputs) that *instructs* the agent to execute a runbook can never be
  satisfied: the agent meta-tool catalog contains **no** runbook-execution
  tool (`meho.runbook.start` / `meho.runbook.next` are operator MCP tools,
  and confirm-gated steps require a human answer by design). Without the
  guard the loop reached the model with no way to act, took **zero** tool
  calls, and reported `succeeded` with a hallucinated explanation ("the
  agent `<slug>` is not available in this tenant") — misleading run-outcome
  telemetry the operator then chases. The guard applies to all three entry
  points (`run`, `run_scheduled` via `_launch_scheduled_run`, and
  `stream_events`, where it surfaces as one terminal `error` frame plus
  the failed row).

  Detection (`find_runbook_instruction` in `agent/run.py`) matches
  instruct-shaped references only — an imperative verb (use / run /
  execute / follow / start / apply / perform / invoke) followed by
  `runbook [template] [<slug>]` or `<slug> runbook`. Every raw regex hit
  is then dispositioned against three false-positive guards before it can
  refuse the run: a **negation** guard (a verb preceded in the same clause
  by not / never / don't / avoid / without / cannot / …n't is a
  prohibition, not an instruction), a **third-person-subject** guard
  (operators / users / we / they / people directly before the verb is
  prose about someone else acting), and a **noun-compound** guard (a bare
  unquoted, non-slug-structured word right after `runbook` — templates /
  syntax / steps / linter / review — heads a noun compound *about*
  runbooks, unless it is a clause-continuation word like *to* / *then*,
  which keeps "use a runbook to remediate" an unnamed instruction). All
  hits in all texts are scanned (`finditer`), so a rejected mention cannot
  mask a real later instruction. A bare English word only counts as a
  *named* slug when quoted or slug-structured (digit / dot / hyphen).
  This is a best-effort boundary guard, not a parser: a phrasing it
  misses degrades to the old behaviour; a phrasing it catches turns a
  fabricated success into an honest typed failure.

  Capability (`toolset_admits_runbook_execution` in `agent/toolset.py`)
  is intersected explicitly: `RUNBOOK_EXECUTION_META_TOOL_NAMES` is an
  (intentionally empty) subset of the meta-tool catalog. A future
  agent-executable runbook tool must be listed there for the guard to
  admit definitions that carry it — and would still have to respect the
  human-confirm contract on `verify.type: confirm` steps.

Callers probe the refusal on the run outcome object
(`error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)` /
`SCHEDULED_RUN_NO_INPUT_CLASS`), not by parsing free text; `output` stays
`NULL` on refused runs.

### Server-side sync timeout

`agent_sync_timeout_seconds` (settings; default 30s, `AGENT_SYNC_TIMEOUT_SECONDS`)
bounds how long a sync run holds the HTTP connection. `run` awaits
`asyncio.wait_for(asyncio.shield(task), timeout)` — the shield means the
timeout abandons the *wait*, not the run, so a long run degrades to a
pollable async run rather than being cancelled.

## Composition — agent invokes agent (T5 #812)

A running agent can invoke **another** agent definition in its tenant as a
child run, via the `invoke_agent` meta-tool (in `meho_backplane/agent/invoke.py`).
From MEHO's view a child run is just another governed call: it resolves through
the same identity, the same RBAC-filtered toolset, the same dispatch + audit
machinery. There is no "tier" concept in MEHO — a consumer's harness may
escalate a cheap-tier agent to a deep-tier agent, but MEHO sees one agent run
invoking another.

`invoke_agent` is **off by default**. It is appended to a built agent only when
the `PydanticAgentRun` carries a `child_agent_resolver`. The live `AgentInvoker`
(T7 #1067) wires it: its default runtime injects `_resolve_child_definition`
(the tenant-scoped `AgentDefinitionService` lookup), `_record_child_run` (the
`agent_run` lineage recorder), and `_finalize_child_run` (T8 #1087 — the
terminal-state finalizer), and binds `current_agent_run_id_var` to each run's
id, so a run started from the deployed REST/MCP/CLI surface can invoke another
agent, the child is recorded against its parent, and the child row reaches
`succeeded` / `failed` rather than staying stuck `running`. A runtime built
without a resolver (the T1/T3 surface, and tests that don't opt in) is unchanged.

### Two independent bounds keep a cascade from escaping

A naive agent-invokes-agent surface is the textbook runaway-cost foot-gun (a
definition that invokes itself, directly or transitively, spawns an unbounded
chain of LLM runs). The mechanism bounds it on two axes; a cascade terminates on
whichever it hits first:

- **Depth** — the *height* of the invocation tree. A per-task contextvar
  (`agent_invoke_depth_var`) tracks how many `invoke_agent` frames the current
  asyncio task is nested inside. The tool pre-increments + checks it against
  `Settings.agent_invoke_max_depth` (default 4, env `AGENT_INVOKE_MAX_DEPTH`)
  *before* the child run starts, so an over-depth invocation never spends. This
  mirrors the composite-recursion cap exactly (`composite_depth_var` +
  `Settings.composite_max_depth`).
- **Budget** — the *total turn count* across the whole cascade. The child loop
  is driven with `usage=ctx.usage` (Pydantic AI's budget-propagation knob), so
  the parent's `RunUsage` accumulator is shared with the child. The shared
  `UsageLimits(request_limit=...)` is enforced against the running total, so a
  deep-but-narrow cascade and a shallow-but-wide one both trip the same budget.
  The framework raises `UsageLimitExceeded`, surfaced as `AgentRunError`.

An over-depth invocation (and an unresolvable agent name, and a failed child
run) surfaces to the model as a `ModelRetry` — a structured, agent-reasonable
error it can recover from ("answer directly or stop"), not a tool-execution
crash. The depth ceiling is a deterministic termination condition the model
never controls.

### Lineage — the cascade tree is reconstructable

The child run is recorded as a child of the parent in two parallel lineages:

- **Run lineage** — the child `agent_run` row's `parent_run_id` points at the
  parent run's id, and its `trigger` is `agent-invoked` (`AgentRunTrigger`).
  Recording the row is delegated to an injected `ChildRunRecorder` callback (the
  T4/T6 surface owns the DB session); a companion `ChildRunFinalizer` callback
  (T8 #1087) closes the recorded row to `succeeded` (with the child's output) or
  `failed` (with the error) after the child loop, mirroring the parent run's
  `_finalize_run` (load fresh → `succeed_run` / `fail_run` → commit, swallowing
  `IllegalTransitionError`). When no recorder is wired (the pure in-process path)
  the depth + budget bounds still apply, the row is just not persisted (and there
  is nothing for the finalizer to close).
- **Session lineage** — `current_agent_run_id_var` carries the current run's id
  for the duration of a child invocation, so a nested `invoke_agent` reads it as
  the next child's `parent_run_id` and per-tool-call audit rows correlate to
  their run.

### Why a tool factory, not a standalone service

The acceptance criterion is that a *running agent* can invoke another — so the
mechanism is a registered tool, reachable from inside the loop. The factory
shape (`make_invoke_agent_tool(resolver, child_runner, recorder, finalizer)`)
injects the `child_runner` (`PydanticAgentRun.run_child`, which owns the
framework `Agent` construction + the `usage=ctx.usage` call, keeping
`pydantic_ai` confined to `agent/run.py`) plus the optional `recorder` /
`finalizer` (the durable child-row create / close hooks the live invoker owns)
without `invoke.py` importing the framework's loop driver or the DB session.

## Awaiting-approval resume (T9 #1117)

When the agent's `call_operation` tool reaches an op with
`requires_approval=True`, the dispatcher parks the dispatch durably (see
[approvals](approvals.md)) and returns an `awaiting_approval` envelope rather
than executing. Until #1117 the only path that resumed the run was the REST
`POST /api/v1/approvals/{id}/approve` endpoint with the original `params` —
the human-driven express lane that re-dispatches inline. Every other operator
surface (`/decide`, MCP `meho.approvals.{approve,reject}`, CLI, wall-monitor)
captures the decision durably and publishes `approval.{approved,rejected}` on
the broadcast feed, but did **not** re-dispatch. Without an agent-side
resume substrate, an agent run that bridged a `requires_approval` op via any
path other than REST `/approve+params` was dead-on-arrival: the operator
could approve, but the agent run never found out.

The agent runtime closes that gap with a wrapped `call_operation`. On
`status="awaiting_approval"`, the wrapper:

1. **Subscribes** to the per-tenant Valkey stream (`meho:feed:{tenant_id}`)
   via `XREAD BLOCK`, filtered to the request's own `approval_request_id`.
   The wait is in `meho_backplane/agent/approval_wait.py`; the per-tenant
   stream is the same one the SSE feed and `meho.broadcast.watch` read.
2. **On approval** — re-invokes the dispatcher with `_approved=True` and the
   original in-memory `params` (passed through `call_operation_with_approval`
   in `operations/meta_tools.py`). The dispatcher's gate-bypass path skips
   the policy gate; the durable approval-decision row is the authorization.
3. **On rejection** — returns the original `awaiting_approval` envelope to
   the model annotated with `extras["error_code"] = "approval_rejected"`
   and `extras["decision"] = "rejected"` plus a rewritten `error` message,
   so the agent's model sees a structured tool result it can reason about.
4. **On timeout / broadcast outage** — returns the envelope annotated with
   `extras["error_code"] = "awaiting_approval_timeout"` so the model can
   distinguish "still pending, timed out" from "decision happened". The
   durable decision row remains the source of truth; the agent can query
   approval status or re-issue.

The wait cap is `Settings.agent_approval_wait_timeout_seconds` (default
1800s = 30 min, env `AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS`). It bounds how
long an agent loop ties up its turn budget on a forgotten review; pick a
value long enough for human review across timezone-distant teams.

### Operator / agent split

The substrate preserves the operator/agent split G11.2 established:

| Path | Decision capture | Re-dispatch |
|---|---|---|
| REST `/approve` with `params` | inline | inline (human as operator + agent) |
| REST `/decide`, MCP, CLI | durable row + broadcast | **agent runtime via wait+wrap** |

For the **agent-run** case this Task targets, the agent-side in-memory
params are authoritative: the live `call_operation` wait holds them and
re-dispatches from there, so `#1117` deliberately did not store `params`
on the approval row. Resuming agent runs whose process died between
dispatch and approval is explicitly out-of-scope for v1.

The **direct operator op** case is different — there is no in-process
wait holding the params, so a parked direct op approved by id alone
(`/decide`, MCP, CLI) had nothing to re-dispatch. #1503 (G0.20-T3) adds a
nullable `approval_request.params` column (migration 0036) so a direct
op's stored params drive the post-approval re-dispatch on any surface.
That column is `run_id`-gated at the re-dispatch sites: an agent-run
request (`run_id` set) is never re-dispatched from `/decide`/MCP — the
broadcast-driven runtime resume above remains its only re-dispatch path,
so this section's behaviour is unchanged. The params column is internal
re-dispatch input only and is never surfaced on a read view or broadcast
frame. See [approvals.md § G0.20-T3](approvals.md#g020-t3--execute-a-parked-direct-op-on-approve-via-every-surface-1503).

### Audit attribution

The resumed dispatch is recorded under the agent principal (the original
caller), with the approval-decision audit row holding the human reviewer's
identity. The two-row decision audit invariant (`approval.request` +
`approval.decision`) sits alongside the dispatch audit row from the
re-dispatch, so the chain reads: agent attempted op → policy gate parked
→ operator decided → agent resumed → op executed. Every row is
correlated by `approval_request_id`.

### Where it lands

- **`agent/approval_wait.py`** — `wait_for_approval_decision(tenant_id,
  approval_request_id, timeout_seconds)` (the read-side primitive) +
  `resume_or_surface_awaiting_approval(...)` (the agent-facing entry
  point that branches on decision).
- **`agent/run.py`** and **`agent/toolset.py`** — the wrapped
  `call_operation` tool. Both the T1 default surface (no toolset) and
  the T3 resolved surface go through the resume substrate; the wrapping
  is duplicated rather than factored out because the two adapters bind
  arguments slightly differently and the wrap is one branch each.
- **`operations/meta_tools.py`** — `call_operation_with_approval(operator,
  arguments)`, the re-dispatch entry point. Threads `_approved=True`
  into the same body `call_operation` uses; not part of the public
  REST/MCP surface.

## Dependencies

- **`pydantic-ai-slim[anthropic,bedrock]`** (pinned in
  `backend/pyproject.toml`) — the loop framework + the Anthropic
  provider (`[anthropic]`) + the Bedrock Converse provider (`[bedrock]`,
  G11.5-T2 #1076; pulls in `boto3`). Both providers are confined to
  `meho_backplane.agent.run` (legacy `default_model_factory`) and
  `meho_backplane.agent.models` (the per-tenant builders).
- **`anthropic`** — the Anthropic SDK, pulled in transitively from
  `[anthropic]` and used only inside `default_model_factory` /
  `anthropic_backend_builder` (lazy-imported, so processes that never
  run an agent against Anthropic do not load it).
- **`boto3` / `botocore`** — pulled in transitively from `[bedrock]`,
  used only inside `bedrock_backend_builder` (lazy-imported on the same
  pattern; the public Bedrock endpoint is `bedrock-runtime.<region>.
  amazonaws.com`).
- **`meho_backplane.operations.meta_tools`** — `call_operation`,
  `list_operation_groups`, `search_operations`: the existing dispatch entry
  points the loop's tools call.
- **`meho_backplane.mcp.registry.role_at_least`** — the role-rank comparison
  the toolset resolver reuses for the identity-permission side of the
  intersection (single-source with the MCP surface's RBAC filter).
- **`meho_backplane.auth.operator.Operator` / `TenantRole`** — the principal
  injected as the framework dependency, and the role gated against the
  meta-tool floors.
- **`meho_backplane.settings`** — `anthropic_api_key` (fail-closed:
  empty means the Anthropic builder raises) and `agent_default_model`
  (the pinned Anthropic model id, never a `-latest` tag). G11.5-T2
  added `bedrock_region` (empty = boto3 region chain; explicit pin
  otherwise) and `bedrock_default_model` (the pinned full Bedrock
  model id, geo-prefixed and `-v1:0`-suffixed) for the Bedrock
  builder; AWS credentials follow boto3's standard chain (env vars
  / IRSA role / instance profile / shared profile) and are *not*
  surfaced as backplane settings of their own. A multi-provider deploy
  that routes some tiers to Anthropic and others to Bedrock works with
  the union of both env-var surfaces.

## Why the model factory, not the `LlmClient` seam

The existing `LlmClient` Protocol (`meho_backplane.operations.ingest`) is
shaped for one-shot JSON completion — `generate_json(system_prompt,
user_prompt, ...) -> str` — the right shape for the spec-ingestion grouping
pass, the wrong shape for a multi-turn tool-use loop, which needs the full
Messages API (tool calls, tool results, repeated turns). Pydantic AI drives
its loop through a framework `Model`, so the agent seam mirrors the
*pattern* of `LlmClientFactory` (an injected, fail-closed factory) rather
than the one-shot method. The G11 initiative shipped against Anthropic;
G11.5-T1 layered a per-tenant resolver on top of that pattern.

## Per-tenant tier→Model resolver (G11.5-T1 #1075)

The zero-arg `ModelFactory` shape was right for one-deploy → one-provider.
It loses the two pieces multi-provider routing needs to honour: *which
tenant* is running the agent, and *which logical tier* (`triage` /
`investigate` / `summarize`) the definition asks for. G11.5-T1 added the
`ModelResolver` protocol in `meho_backplane/agent/models.py`, the
architectural sibling of the connectors' fingerprint resolver:

```python
resolver.resolve(operator, tier) -> pydantic_ai.models.Model
```

`PydanticAgentRun` now takes an optional `model_resolver`. When *both* the
runtime carries a resolver *and* the definition names a tier, the
resolver builds the model; otherwise the legacy `model_factory` runs.
This dual path keeps every existing test (which injects
`model_factory=lambda: FunctionModel(...)`) unmodified — definitions
without a tier still resolve through the factory.

### The three gates

For every `resolve(operator, tier)` call the resolver checks, in order:

1. **Tenant policy** — `policies[tenant_id]` (falling back to the
   `__default__` policy when the tenant has no explicit row) maps each
   `AgentTier` to a `TierMapping(backend_id=...)`. A tier the policy
   doesn't cover raises `BackendNotConfiguredError`.
2. **Egress** — when the policy carries `allow_egress=False` (the
   air-gapped posture), the resolver refuses to materialise any backend
   flagged `is_saas_egress=True` and raises `EgressViolationError`. The
   per-backend flag (not a name-string match) is what decides; an
   on-prem Bedrock-via-AWS-PrivateLink deployment can register the same
   `bedrock` builder with `is_saas_egress=False`.
3. **Capabilities** — each backend declares `BackendCapabilities`
   (`supports_tools`, `supports_streaming`, `supports_prompt_cache`,
   `tool_format`). The agent runtime always needs tools (the loop is
   tool-use), so a backend with `supports_tools=False` mapped to any
   tier raises `CapabilityMismatchError`. Future capability-aware tiers
   (a no-tools "free-text summary" tier) reuse the same shape.

### What ships in T1 + T2 vs C4-c/d

T1 (#1075) shipped the resolver + capability flags + the **Anthropic
backend builder** (lifted from the old `default_model_factory`, fail-
closed on missing key). The recovery shape `default_anthropic_policy()`
+ `default_anthropic_backends()` reproduces the pre-resolver single-
tenant behaviour — every tier under the default tenant policy routes
to Anthropic.

T2 (#1076) added the **AWS Bedrock Converse backend builder** alongside:
`bedrock_backend_builder()` constructs a
`pydantic_ai.models.bedrock.BedrockConverseModel` against a
`BedrockProvider` (boto3 region + credential chain). The shipped
registration `default_bedrock_backends()` exposes the builder under the
id `"bedrock-anthropic"` with `is_saas_egress=True` — public Bedrock
endpoints traverse the public internet, so an air-gapped tenant
brokering Bedrock over AWS PrivateLink / VPC endpoints layers a second
registration under a different id (`"bedrock-anthropic-privatelink"`,
say) with `is_saas_egress=False`. The `[bedrock]` extra (boto3) lives
on the same pydantic-ai-slim pin as `[anthropic]`, so every wheel
already ships both; the lazy function-local imports in each builder
keep an unused backend's dependencies out of the import graph.

**The Bedrock caveat that drives `tool_format="converse"`.** Pydantic
AI's Bedrock path is the **Converse API** (boto3), *not* the
`anthropic[bedrock]` adapter. The two look like "Claude over AWS" from
a tenant-facing distance, but route tool calls through different wire
shapes (Bedrock `toolSpec` vs. Anthropic-native XML), so the capability
flag records the difference. A future tool-format adapter (initiative
#806 §C4) branches on the `tool_format` string rather than inferring
from the underlying model family — Claude over Anthropic-direct and
Claude over Bedrock are different format domains.

**Per-model prompt-caching on Bedrock.** The default Bedrock
registration sets `supports_prompt_cache=True` because it targets the
Anthropic-on-Bedrock family, which the
`pydantic_ai.providers.bedrock.BedrockModelProfile`
`bedrock_supports_prompt_caching` allow-list covers. A deploy that
registers a *non*-Anthropic Bedrock model (Amazon Nova, Mistral,
Cohere) registers it under a separate backend id with a copy of
`bedrock_capabilities` flipping `supports_prompt_cache=False` — the
resolver and the cost-attribution path (#1079) read the per-
registration flag, not the model id.

OpenAI-compatible / vLLM / Ollama (#1077 — landed) and VCF Private AI
Foundation (#1078) land their own `BackendBuilder` registrations on
the same pattern. They are deliberately not eagerly imported in
`models.py`'s default policy helpers — an eager import would break the
module on a deployment without the corresponding extra. The `[openai]`
extra **is** pinned now (see #1077 below) but its builder still imports
lazily inside the closure so an Anthropic-only deploy never loads the
`openai` wheel.

## OpenAI-compatible backend (G11.5-T3 #1077)

The OpenAI-compatible backend covers three deployment shapes the
Initiative #806 §C4 calls out: **OpenAI SaaS** (`api.openai.com`),
**vLLM** on-prem (a Python inference server exposing the OpenAI Chat
Completions wire format under `/v1`), and **Ollama** local (the same
wire format with a few documented quirks). All three share the
transport — pydantic_ai's `OpenAIChatModel` + `OpenAIProvider` — and
differ on which sub-features the underlying engine actually
implements, surfaced through `OpenAIModelProfile` flags.

### Three knobs, one shape

The vendor-specific quirks are wired through three pre-built profile
factories in `meho_backplane.agent.models`:

| Vendor | `openai_supports_strict_tool_definition` | `openai_chat_supports_multiple_system_messages` |
|---|---|---|
| `OpenAICompatVendor.OPENAI` | `True` (default) | `True` (default) |
| `OpenAICompatVendor.VLLM` | **`False`** — engine ignores the strict flag (vLLM tool-calling docs) | `True` |
| `OpenAICompatVendor.OLLAMA` | **`False`** — `openai` compat layer ignores the strict flag | **`False`** — Ollama collapses multiple `role=system` turns |

The `json_schema_transformer` knob the issue body names stays at the
framework's `None` default for all three vendors — none of them
requires a per-call schema rewrite at the time of this slice.

`BackendCapabilities` for the OpenAI-compat surface:

- `supports_tools=True` — every vendor honours the tool-use loop.
- `supports_streaming=True` — same.
- `supports_prompt_cache=False` — none of the three exposes the
  Anthropic-style `cache_control` knob. OpenAI's automatic input
  caching is opaque to the client; vLLM/Ollama have no equivalent.
  The cost-attribution layer (#1079) reads this flag to decide
  whether to model a per-message cache discount.
- `tool_format="openai"` — the wire format every OpenAI-compat
  surface speaks.

### Two builder shapes

```python
# Per-backend, fully parameterised — the multi-endpoint case
builder = openai_compat_backend_builder(
    vendor=OpenAICompatVendor.VLLM,
    model_id="meta-llama/Llama-3.1-8B-Instruct",
    base_url="http://vllm.internal:8000/v1",
    api_key=vault_secret,
)
backends = {
    "vllm-on-prem": (builder, openai_compat_capabilities, False),  # is_saas_egress=False
    ...
}

# Settings-driven default — the single-knob single-tenant case
model = default_openai_backend_builder()
```

The explicit `openai_compat_backend_builder(...)` is the multi-tenant
shape: each on-prem endpoint gets its own backend id, its own
`base_url`, its own `api_key`. The settings-driven
`default_openai_backend_builder()` is the convenience path — it reads
`openai_api_key` / `openai_base_url` / `openai_default_model` from
`Settings` and picks a vendor profile from the base URL host hint
(URL contains `ollama` → Ollama profile, `vllm` → vLLM, else
OpenAI). Both builders are **lazy**: pydantic_ai's `openai` provider
imports inside the closure, so a deploy that registers an OpenAI-compat
backend but never resolves to it never loads the `openai` wheel.

### `base_url` configuration model

The OpenAI-compat builder takes its endpoint and credential from one
of two sources, in order of authority:

1. **Per-tenant secret + per-backend builder.** A
   `openai_compat_backend_builder(base_url=..., api_key=...)` call
   constructs a closure that captures the values verbatim; the
   resolver registers it under a tenant-specific backend id and the
   tenant's `TenantModelPolicy.tiers` map points at that id. This is
   how a real multi-tenant deploy wires per-tenant on-prem endpoints
   — each tenant's `base_url` comes from its row in the tenants table
   (or from Vault, when the credential is per-tenant), the
   `api_key` from a Vault-issued per-tenant token. The builder never
   reaches into `Settings`.
2. **Settings-driven default.** `default_openai_backend_builder()`
   reads `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_DEFAULT_MODEL`
   from process environment — wired the same way `ANTHROPIC_API_KEY`
   is on Helm deploys (G0.18-T10 #1363): a first-class chart `agent.*`
   block, `secretKeyRef` only, optional ExternalSecret rendering via
   `eso.agent.enabled`. Empty `OPENAI_API_KEY` is fail-closed: the
   builder raises `AgentRunError` rather than starting a loop with no
   credentials. Use this only when the whole deploy talks to one
   OpenAI-compat endpoint — the moment you need per-tenant routing,
   switch to builder #1.

### Egress + the `is_saas_egress` flag

The resolver's egress check (`allow_egress=False` refuses any backend
flagged `is_saas_egress=True`) is honoured uniformly across backend
kinds: OpenAI SaaS at `api.openai.com` is `is_saas_egress=True`, an
on-prem vLLM / Ollama / VCF PAIF endpoint is `is_saas_egress=False`.
A no-egress tenant policy can route every tier through an
OpenAI-compat backend without tripping `EgressViolationError`, as
long as each registered backend's egress flag matches the endpoint's
actual posture. The flag is set at registration time, not derived
from the URL — operators are responsible for declaring the truth
about the endpoint they are pointing at.

### Failure mode unification

Every `ResolverError` subclass raised at `_build_agent` time is wrapped
in `AgentRunError` by `PydanticAgentRun._resolve_model`, so callers
catch the seam's one exception type regardless of which precise
resolver mismatch fired. The original `ResolverError` is preserved as
`__cause__` so an operator's log read sees the policy detail.

## VCF Private AI Foundation backend (G11.5-T4 #1078)

VCF Private AI Foundation (PAIF) is VMware's air-gapped on-prem
inference platform — the **zero-egress** target the Initiative #806
DoD names. PAIF reuses the OpenAI-compat seam from #1077 with two
deviations that matter:

1. **Non-standard sub-path.** PAIF mounts the OpenAI-compatible
   API at `/api/v1/compatibility/openai/v1/` (Broadcom developer
   docs — pinned as `VCF_PAIF_OPENAI_COMPAT_BASE_PATH`), not the
   bare `/v1` vLLM exposes or the `api.openai.com/v1` SaaS shape.
2. **OpenID bearer auth.** PAIF requires an OAuth 2.0 / OIDC
   access token in the `Authorization` header — not an API key.
   The Broadcom developer docs name Authorization Code with PKCE
   as the preferred interactive grant; for the backplane (a
   service-to-service caller), the bundled OIDC provider runs the
   `client_credentials` grant against the IdP's token endpoint.

The wire format is OpenAI Chat Completions verbatim, so the
`OpenAIChatModel` + `OpenAIProvider` stack from #1077 is reused. The
PAIF profile (`vcf_paif_chat_profile`) is bit-equivalent to the vLLM
profile — strict-tool-def off, multi-system on — because the underlying
PAIF engine is vLLM for chat completions (Broadcom techdocs;
embeddings use Infinity, CPU fallback uses llama.cpp, but only the
chat-completions surface is in scope for the agent runtime today).

### Bearer-token provider (lazy callable, not static api_key)

The auth pattern shape is the design decision worth understanding:

- The framework default would be to pass `api_key="<bearer>"` to
  `OpenAIProvider` and rebuild the provider whenever the IdP
  rotates the token. That works, but it re-instantiates the
  underlying `httpx.AsyncClient` on every resolver call —
  losing connection pooling and forcing a TCP+TLS handshake to
  PAIF on every agent run.
- `openai>=2.0` accepts `api_key: str | Callable[[], Awaitable[str]] | None`
  natively on `AsyncOpenAI`. The PAIF backend takes the
  `Callable` path: one long-lived `AsyncOpenAI` client wired with
  a token-provider callable that re-resolves on every request.
  Token rotation is transparent — no resolver rebuild required.

```python
provider = vcf_paif_bearer_provider(
    token_url="https://kc.airgap.local/realms/meho/protocol/openid-connect/token",
    client_id="meho-backplane",
    client_secret=vault_secret,
    scope="paif",  # optional
)
builder = vcf_paif_backend_builder(
    model_id="openai:meta-llama/Llama-3.1-8B-Instruct",
    base_url="https://pais.airgap.local/api/v1/compatibility/openai/v1/",
    bearer_token_provider=provider,
)
backends = {
    "vcf-paif": (builder, vcf_paif_capabilities, False),  # is_saas_egress=False
}
```

The bundled `OidcClientCredentialsTokenProvider` caches the access
token + an absolute monotonic expiry (`time.monotonic() + expires_in
- refresh_skew_seconds`) under a `threading.Lock` — concurrent
agent runs against the same provider don't double-post the grant.
`refresh_skew_seconds` defaults to 30 s: long enough to mask a slow
IdP, short enough not to waste most of a typical 5–15 min lifetime.

Why `client_credentials` and not Authorization-Code-with-PKCE (the
PAIF docs' "preferred" grant): the backplane is a service-to-service
caller, not an interactive user. Per-tenant authorization is enforced
one layer up — the tenant policy maps the tier to this PAIF backend —
not by per-call token issuance. A future per-tenant token mode would
supply a *different* `BearerTokenProvider` callable; the builder
surface stays unchanged.

Why no refresh-token plumbing: the `client_credentials` grant in
OAuth 2.0 does not return a refresh token (RFC 6749 §4.4.3). The
recovery path is "re-issue an access token by re-running the grant"
— exactly what `_fetch_token` does on cache miss.

### Settings-driven default

The convenience path mirrors `default_openai_backend_builder` /
`anthropic_backend_builder`:

```python
model = default_vcf_paif_backend_builder()
```

Reads `vcf_paif_base_url` / `vcf_paif_model` / the OIDC config
(`vcf_paif_oidc_token_url` / `vcf_paif_oidc_client_id` /
`vcf_paif_oidc_client_secret` / `vcf_paif_oidc_scope`) from
`Settings`. Fail-closed: any of the four required settings empty
raises `AgentRunError` naming every missing key — the operator's
fix is one `helm upgrade` away. Multi-PAIF deploys (per-tenant
routing to different appliances) construct each backend via
`vcf_paif_backend_builder(...)` explicitly and never touch the
settings-driven default.

### Egress posture (the on-prem invariant)

PAIF is on-prem by definition — the registration triple must
declare `is_saas_egress=False`. An air-gapped tenant
(`allow_egress=False`) routing every tier to a PAIF backend
resolves without tripping `EgressViolationError`. A regression
that mis-registered PAIF with `is_saas_egress=True` would
fail-close on resolve — the resolver enforces the egress flag,
not URL-parsing — proving the egress contract is end-to-end
robust regardless of which backend kind is misregistered.

### Token-acquisition failure modes

A separate typed error — `TokenAcquisitionError` — surfaces IdP
failures distinctly from `ResolverError`:

- IdP returns a non-2xx (invalid client id/secret, grant not
  enabled, IdP down): message names the IdP's `error` field
  when present (`invalid_client`, `invalid_grant`, …) so the
  operator's log read maps to a Keycloak / Okta / Authentik
  client config without spelunking through logs.
- IdP returns 200 with a malformed body (no `access_token` or
  `expires_in`): typed error rather than silent fallthrough to
  `None`.
- Network unreachable: same typed surface, with the underlying
  `httpx.HTTPError` preserved as `__cause__`.

`TokenAcquisitionError` is deliberately **not** wrapped in
`ResolverError` because it is a runtime condition (token endpoint
down, secret rotated out-of-band) discovered after resolve, not a
configuration mismatch discovered at resolve time. The agent loop
surfaces it as the terminal `AgentRunEventKind.ERROR` event with the
typed reason preserved.

### Cross-repo deployer recipe

Operator-facing setup — provisioning the OIDC client in the IdP
realm, the Helm values for the OIDC env vars, the
per-environment values pattern — is at
[`docs/cross-repo/vcf-paif-deployment.md`](../cross-repo/vcf-paif-deployment.md).
That doc is the consumer-facing handshake spec; this section is
the architecture grounding it links back to.

## Testing

- `backend/tests/test_agent_run.py` — unit tests against a deterministic
  `FunctionModel` (no network): the loop calls `call_operation` against a
  seeded typed op, the turn budget caps a runaway loop, `output_type` is
  validated, the operator is threaded into tool calls, the default model
  factory fails closed without a key, and (T3) a toolset-driven definition
  registers the resolved tools, a meta-tool omitted from the spec is absent
  from the model's surface, and a `read_only` identity gets an empty surface.
- `backend/tests/test_agent_model_resolver.py` (G11.5-T1 + T2) — unit
  tests for the per-tenant tier→Model resolver: a tenant policy picks
  the configured backend per tier; a no-egress tenant refuses a SaaS
  backend; a no-tools backend is refused; the default-tenant policy
  routes every tier to Anthropic; the Anthropic builder fails closed
  without a key; an unknown backend id raises
  `BackendNotConfiguredError`; the `PydanticAgentRun` seam routes
  through the resolver when both a resolver and a `tier` are set,
  falls back to the factory otherwise, and wraps `ResolverError`s in
  `AgentRunError`. G11.5-T2 adds: Bedrock capability flags declare
  `tool_format="converse"`; `default_bedrock_backends()` registers the
  `bedrock-anthropic` id and builds a `BedrockConverseModel`; the
  Bedrock builder fails closed when boto3 cannot resolve a region; an
  air-gapped tenant refuses the default (SaaS) Bedrock registration
  but routes through a PrivateLink-flagged sibling registration
  cleanly; a tenant policy can split tiers across Anthropic + Bedrock
  by layering both default registrations.
- `backend/tests/test_agent_openai_compat_backend.py` (G11.5-T3) — unit
  tests for the OpenAI-compat builder, per-vendor profile dispatch
  (OpenAI / vLLM / Ollama), the air-gapped + on-prem registration path,
  the settings-driven default builder's host-hint heuristic, and
  fail-closed posture without `OPENAI_API_KEY`.
- `backend/tests/test_agent_vcf_paif_backend.py` (G11.5-T4) — unit tests
  for the VCF PAIF builder and the bundled OIDC token provider: the
  vLLM-equivalent profile flips strict-tool-def off; the `client_credentials`
  grant body is form-encoded with the right parameters; the cached access
  token survives the skew window and gets re-acquired past it; IdP
  non-2xx + malformed-200 + network errors surface as `TokenAcquisitionError`;
  the air-gapped tenant resolves all three tiers to PAIF with **zero**
  SaaS-host traffic; a mis-flagged `is_saas_egress=True` PAIF registration
  still fails closed for an air-gapped tenant; the settings-driven default
  fails closed naming every missing setting.
- `backend/tests/test_agent_toolset.py` — unit tests for the resolver /
  adapter directly (no model run): the spec ∩ identity-perms intersection
  (including the read_only-floor exclusion), operator threading, tool input
  schemas reflecting the meta-tool schemas, the connector allow-list yielding
  a `ModelRetry`, and spec validation.
- `backend/tests/integration/test_agent_run_anthropic.py` — opt-in
  real-Anthropic loop (skipped unless `ANTHROPIC_API_KEY` is set; marked
  `slow`). Proves the seam drives a live model that calls a real operation
  end to end.
- `backend/tests/test_agent_invocation.py` (T4) — the invoker + seam event
  stream against a `FunctionModel`: sync blocks and returns, a long sync run
  converts to async, async returns a handle then poll succeeds, poll reads
  the durable row after store eviction, the event stream emits turn /
  tool-call / tool-result / final, and the missing / cross-tenant / disabled
  error paths.
- `backend/tests/test_api_v1_agent_runs.py` (T4) — the REST surface end to
  end (sync 200, async 202 + poll over an httpx ASGI loop for the durability
  contract, SSE frames, RBAC 403, tenant-scoping 404s, disabled 409, durable
  row persisted).
- `backend/tests/test_mcp_tools_agent_runs.py` (T4) — `meho.agents.run` +
  `meho.agents.run_status` round-trip, error mapping, and operator-role
  visibility.
- `cli/internal/cmd/agent/run_test.go` (T4) — the `run` / `run-status` /
  `run-events` verbs against an `httptest` server (path builders, sync +
  async rendering, SSE frame parsing with heartbeat skipping).

## Known issues / future work

- `AgentRun.stream` (T1) ships a minimal single-chunk contract (it awaits the
  run and yields the final answer). The richer turn / tool-call / final
  stream is `AgentRun.stream_events` (T4), consumed by the SSE route. Pure
  token-by-token (intra-turn) streaming via the framework's `run_stream`
  per-part deltas is not surfaced — node-level events are the granularity.
- The T4 invocation surface maps a definition's persisted `output_schema`
  (JSON Schema) to a free-text run, not a validated structured output: the
  invoker passes `output_type=None`, so a run with a stored output schema
  returns its loop's free-text answer recorded as `{"text": ...}`.
  Synthesising a runtime Pydantic model from JSON Schema is the seam's
  `output_type` contract, deferred — see `AgentInvoker._to_agent_definition`.
- The run store is per-worker in-memory: a background async run anchored in
  one worker's store is not visible to (or pollable from a still-in-memory
  task on) another worker, though the durable `agent_run` row makes poll
  work cross-worker. Cancellation of an in-flight loop is the T6
  `cancel_run` path (records durable intent); wiring the loop to observe it
  at a turn boundary is future work.
- Cancellation propagation parent→child is not wired: a running child does not
  observe the *parent's* `cancel_run` at a turn boundary and stop. The child's
  own lifecycle is finalized (below), but a cancel issued against the parent
  while a child is mid-loop only terminates the parent — a separate task. (A
  child row already in a terminal state when the finalizer runs — e.g. a direct
  cancel against the child — is handled: the finalizer swallows the resulting
  `IllegalTransitionError`.)
- The identity-permission side of the intersection is the tenant role today.
  When the G11.2 per-op permission model lands, the resolver's role gate is
  the seam to extend (or replace) with the real per-op grant check — the
  intersection shape (spec ∩ identity perms) and the "absent, not denied"
  posture stay the same.

## References

- Goal #800 (G11 agentic ops runtime); Initiative #802 (G11.1); Tasks #808
  (T1 seam), #809 (T2 agent_definition), #810 (T3 toolset resolution),
  #811 (T4 invocation surface), #812 (T5 composition), #813 (T6 `agent_run`
  record).
- Pydantic AI: agent concepts (`UsageLimits`, `run`, `deps`/`RunContext`,
  `output_type`, budget-aware sub-runs via `usage=ctx.usage`), tool
  registration (`Tool.from_schema`, the `tools=` constructor arg, `ModelRetry`),
  Anthropic model + provider.
- Grounding: `operations/dispatcher.py` (`dispatch`), `operations/meta_tools.py`
  (`call_operation`, `list_operation_groups`, `search_operations`),
  `mcp/registry.py` (`role_at_least`), `auth/operator.py`; composition cap
  precedent `operations/composite.py` (`composite_depth_var` +
  `Settings.composite_max_depth`) and `operations/agent_run.py`
  (`create_run(parent_run_id=...)`).

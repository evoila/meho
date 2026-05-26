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
  / converted-to-async), `GET /api/v1/agents/runs/{handle}` (poll), and
  `POST /api/v1/agents/{name}/run/events` (SSE — `text/event-stream`, the
  G6 broadcast-feed transport shape). The poll/events sub-paths are two
  segments deep so they never collide with the one-segment `/{name}`
  definition-CRUD routes.
- **MCP** (`mcp/tools/agent_runs.py`): `meho.agents.run` (sync/async via an
  `async` arg) + `meho.agents.run_status` (poll). SSE is REST-only; an MCP
  caller polls. Both drive the same `AgentInvoker` singleton, so a run
  started over MCP is poll-able over REST and vice versa.
- **CLI** (`cli/internal/cmd/agent/run.go`, `run_status.go`,
  `run_events.go`): `meho agent run <name> --input … [--async]`,
  `meho agent run-status <handle>`, `meho agent run-events <name> --input …`.

All fronts require the `operator` role and are tenant-scoped via the JWT.

### Why the SSE events route is a POST that starts a fresh run

The issue's literal shape was `GET …/runs/{handle}/events`. SSE needs the
run *input*, and the seam streams a fresh run per connection (the same
fresh-stream-per-connection shape the G6 broadcast feed uses), so the route
is `POST /agents/{name}/run/events` — the input arrives in the body and one
connection drives one run's lifetime. The run is recorded on a durable
`agent_run` row (every frame's `data:` carries the `run_id`), so the
poll-after-stream contract the issue intends still holds.

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

Storing the `params` on the approval row would turn the table into a
re-dispatch primitive holding secrets-bearing call payloads — a security
and audit-surface tradeoff the issue body documents (`#1117` "Why not store
params on approval_request"). The agent-side in-memory params are
authoritative for the live-wait case this Task targets. Resuming agent
runs whose process died between dispatch and approval is explicitly
out-of-scope for v1.

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

- **`pydantic-ai-slim[anthropic]`** (pinned in `backend/pyproject.toml`) —
  the loop framework + the Anthropic provider. Confined to
  `meho_backplane.agent.run`.
- **`anthropic`** — the Anthropic SDK, pulled in transitively and used only
  inside `default_model_factory` (lazy-imported, so processes that never run
  an agent against Anthropic do not load it).
- **`meho_backplane.operations.meta_tools`** — `call_operation`,
  `list_operation_groups`, `search_operations`: the existing dispatch entry
  points the loop's tools call.
- **`meho_backplane.mcp.registry.role_at_least`** — the role-rank comparison
  the toolset resolver reuses for the identity-permission side of the
  intersection (single-source with the MCP surface's RBAC filter).
- **`meho_backplane.auth.operator.Operator` / `TenantRole`** — the principal
  injected as the framework dependency, and the role gated against the
  meta-tool floors.
- **`meho_backplane.settings`** — `anthropic_api_key` (fail-closed: empty
  means the model factory raises) and `agent_default_model` (the pinned
  model id, never a `-latest` tag).

## Why the model factory, not the `LlmClient` seam

The existing `LlmClient` Protocol (`meho_backplane.operations.ingest`) is
shaped for one-shot JSON completion — `generate_json(system_prompt,
user_prompt, ...) -> str` — the right shape for the spec-ingestion grouping
pass, the wrong shape for a multi-turn tool-use loop, which needs the full
Messages API (tool calls, tool results, repeated turns). Pydantic AI drives
its loop through a framework `Model`, so the agent seam mirrors the
*pattern* of `LlmClientFactory` (an injected, fail-closed factory) rather
than the one-shot method. The G11 initiative ships against Anthropic;
multi-provider routing (Bedrock, on-prem OpenAI-compatible, VCF Private AI
Foundation) is G11.5.

## Testing

- `backend/tests/test_agent_run.py` — unit tests against a deterministic
  `FunctionModel` (no network): the loop calls `call_operation` against a
  seeded typed op, the turn budget caps a runaway loop, `output_type` is
  validated, the operator is threaded into tool calls, the default model
  factory fails closed without a key, and (T3) a toolset-driven definition
  registers the resolved tools, a meta-tool omitted from the spec is absent
  from the model's surface, and a `read_only` identity gets an empty surface.
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

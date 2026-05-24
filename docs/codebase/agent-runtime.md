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
loop), the G11.1-T3 toolset resolution layered on it, and the G11.1-T4
public invocation surface (sync + async handle/poll/SSE on REST + MCP +
CLI) built on top. Other G11.1 tasks: agent-definition persistence (T2),
agent-invokes-agent composition (T5), and durable run records (T6 — whose
`agent_run` row the T4 surface reads/writes).

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
- The identity-permission side of the intersection is the tenant role today.
  When the G11.2 per-op permission model lands, the resolver's role gate is
  the seam to extend (or replace) with the real per-op grant check — the
  intersection shape (spec ∩ identity perms) and the "absent, not denied"
  posture stay the same.

## References

- Goal #800 (G11 agentic ops runtime); Initiative #802 (G11.1); Tasks #808
  (T1 seam), #810 (T3 toolset resolution), #811 (T4 invocation surface),
  #813 (T6 `agent_run` record).
- Pydantic AI: agent concepts (`UsageLimits`, `run`, `deps`/`RunContext`,
  `output_type`), tool registration (`Tool.from_schema`, the `tools=`
  constructor arg, `ModelRetry`), Anthropic model + provider.
- Grounding: `operations/dispatcher.py` (`dispatch`), `operations/meta_tools.py`
  (`call_operation`, `list_operation_groups`, `search_operations`),
  `mcp/registry.py` (`role_at_least`), `auth/operator.py`.

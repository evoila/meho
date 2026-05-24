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
loop). Later G11.1 tasks build on it: agent-definition persistence (T2),
toolset resolution (T3), the public sync/async invocation surface (T4),
agent-invokes-agent composition (T5), and durable run records (T6).

## Key types

All public types are re-exported from `meho_backplane.agent`
(`agent/__init__.py`); they are defined in `meho_backplane/agent/run.py`.

- **`AgentDefinition`** — frozen Pydantic model holding the static shape of
  one run: `name`, `system_prompt`, `request_limit` (the turn budget),
  optional `model` id override, and optional `output_type` (a Pydantic
  model class for structured output). T2 will materialise persisted rows
  into this shape.
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
   definition, wires the meta-tools, and launches the bounded loop as an
   `asyncio.Task`. It returns immediately with an `AgentRunHandle`.
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

For T1 the loop is wired with exactly two existing MEHO meta-tools, adapted
from their `(operator, arguments) -> dict` handler shape onto the
framework's `RunContext`-first tool signature in `_register_meta_tools`:

- `call_operation_tool` → `meta_tools.call_operation` (execution)
- `list_operation_groups_tool` → `meta_tools.list_operation_groups`
  (discovery)

The handler *is* the dispatch path REST + MCP use; the agent gets no
special surface. The handler docstrings double as the model-facing tool
descriptions. T3 (#810) replaces this hand-wiring with toolset resolution
that registers the agent identity's full permitted surface (toolset ∩
identity permissions).

## Dependencies

- **`pydantic-ai-slim[anthropic]`** (pinned in `backend/pyproject.toml`) —
  the loop framework + the Anthropic provider. Confined to
  `meho_backplane.agent.run`.
- **`anthropic`** — the Anthropic SDK, pulled in transitively and used only
  inside `default_model_factory` (lazy-imported, so processes that never run
  an agent against Anthropic do not load it).
- **`meho_backplane.operations.meta_tools`** — `call_operation`,
  `list_operation_groups`: the existing dispatch entry points the loop's
  tools call.
- **`meho_backplane.auth.operator.Operator`** — the principal injected as
  the framework dependency.
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
  validated, the operator is threaded into tool calls, and the default
  model factory fails closed without a key.
- `backend/tests/integration/test_agent_run_anthropic.py` — opt-in
  real-Anthropic loop (skipped unless `ANTHROPIC_API_KEY` is set; marked
  `slow`). Proves the seam drives a live model that calls a real operation
  end to end.

## Known issues / future work

- `stream` ships a minimal single-chunk contract in T1 (it awaits the run
  and yields the final answer). Token-by-token streaming via the framework's
  `run_stream` / `iter` node events lands in T4 (#811), where the SSE
  transport lives.
- Run handles are in-memory `asyncio.Task`s. Durable `agent_run` records +
  a session-id lineage key + cancellation are T6 (#813).
- The toolset is hand-wired to two meta-tools. Full toolset resolution is
  T3 (#810).

## References

- Goal #800 (G11 agentic ops runtime); Initiative #802 (G11.1); Task #808.
- Pydantic AI: agent concepts (`UsageLimits`, `run`, `deps`/`RunContext`,
  `output_type`), Anthropic model + provider.
- Grounding: `operations/dispatcher.py` (`dispatch`), `operations/meta_tools.py`
  (`call_operation`, `list_operation_groups`), `auth/operator.py`.

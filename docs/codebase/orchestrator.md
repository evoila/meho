# Orchestrator Agent

## Overview

`OrchestratorAgent` (`meho_app/modules/agents/orchestrator/agent.py`) is the top-level agent that drives an investigation session. It runs a multi-turn dispatch loop: each iteration selects relevant connectors, fans out to specialist agents in parallel, collects their findings, and decides whether to continue or synthesize. Events are streamed to the caller via an `AsyncIterator[AgentEvent]` as they arrive.

## Key types

| Type | Module | Purpose |
|---|---|---|
| `OrchestratorState` | `orchestrator/state.py` | Mutable loop state: findings, iteration count, session metadata |
| `ConnectorSelection` | `orchestrator/state.py` | Routing decision for one connector (id, name, type, max_steps, …) |
| `SubgraphOutput` | `orchestrator/contracts.py` | Terminal result from one specialist agent (findings, status, error_classification) |
| `WrappedEvent` | `orchestrator/contracts.py` | Mid-run streaming event from a specialist, tagged with connector metadata |
| `EventWrapper` | `orchestrator/event_wrapper.py` | Wraps raw `AgentEvent` → `WrappedEvent` for a specific connector |
| `AgentEvent` | `agents/base/events.py` | Typed SSE event surfaced to the API layer |

## Control flow

```
run_streaming()
 ├── ask-mode bypass (session_mode == "ask") → run_ask_mode()
 ├── _build_run_state()       — init OrchestratorState + transcript collector
 └── dispatch loop (while should_continue)
      ├── _decide_next_action()   — LLM routing decision
      ├── _emit_plan_events()     — stream plan events to caller
      ├── _stream_dispatch_round()
      │    └── _dispatch_parallel()              — fan-out to all selected connectors
      │         └── _spawn_agent_tasks()         — one asyncio Task per connector
      │              └── _run_single_agent()     — per-connector worker
      │                   ├── _start_agent_span()         — open per-agent OTEL child span
      │                   ├── _create_specialist_agent()  — DB skill + factory + prompt
      │                   ├── _build_agent_context()      — context dict for specialist
      │                   ├── _stream_agent_events()      — drive specialist run_streaming
      │                   ├── _handle_agent_failure()     — exception → SubgraphOutput
      │                   └── _finalize_agent_span()      — close span, set status/attrs
      ├── _stream_topology_traversal()  — follow-up rounds via topology routing
      └── _stream_completion() / _close_run()
           └── _synthesize_streaming()           — final answer over all findings
                ├── _start_synthesis_span()      — open OTEL synthesis span
                ├── _try_single_connector_passthrough()  — ARCH-03 fast path
                ├── _build_synthesis_prompt()    — memory + skill context + multi-turn
                ├── _stream_synthesis_chunks()   — pydantic-ai streaming with retry
                │    ├── _emit_structured_synthesis_events()  — follow_ups + citations
                │    └── _log_synthesis_usage()  — transcript token logging
                └── _finalize_synthesis()        — set final_answer, close span
```

### ARCH-03 single-connector passthrough

When `_try_single_connector_passthrough` detects exactly one successful connector and no failed ones, it short-circuits the LLM synthesis path: the connector's findings are returned verbatim as a `synthesis_chunk` event with `passthrough=True`. The synthesis LLM is never called, which eliminates latency and avoids re-summarising an already-complete response.

### Streaming synthesis with retry

`_stream_synthesis_chunks` runs a pydantic_ai `Agent` in streaming mode. On transient HTTP errors (429, 500, 502, 503, 529) it retries with exponential back-off (up to 3 retries). When retries are exhausted: if any text was already streamed, it is returned with an interruption notice appended; otherwise `LLMError` propagates to the caller. The blocking `_call_llm` helper is only used as a fallback for truly unexpected exceptions (anything outside `LLMError` / `ModelHTTPError` / `ModelAPIError`). In all paths, accumulated text is written to a caller-supplied `text_out: list[str]` parameter — a workaround for the fact that `return` cannot communicate a value out of an async generator (PEP 525).

## OTEL span hierarchy

```
meho.orchestrator.run            (root, opened in run_streaming)
 ├── meho.orchestrator.dispatch  (one per dispatch round, opened in _dispatch_parallel)
 │    └── meho.orchestrator.agent (one per connector per round, opened in _run_single_agent)
 └── meho.orchestrator.synthesis (one per run, opened in _start_synthesis_span)
```

Spans are opened with `tracer.start_span()` + `use_span().__enter__()` and closed in `finally` blocks to guarantee teardown even on generator exit or unexpected exceptions.

## Context propagation

`_spawn_agent_tasks` calls `contextvars.copy_context()` per task (a fresh snapshot inside the loop, not a shared snapshot outside) before spawning each asyncio `Task`. This copies the active OTEL span context and the transcript collector `ContextVar` into each child task and isolates mutations between siblings, so per-agent spans correctly nest under the dispatch span (not under the previous task's span) and observability events are routed to the right session.

## Key sub-modules

| Module | Role |
|---|---|
| `orchestrator/routing.py` | LLM prompt + decision parsing for connector selection |
| `orchestrator/streaming.py` | `build_agent_prompt` — constructs the per-connector investigation prompt |
| `orchestrator/synthesis.py` | Builds synthesis prompts; fetches memory summaries |
| `orchestrator/synthesis_parser.py` | Parses structured synthesis output (follow-ups, citation map) |
| `orchestrator/topology_routing.py` | Follow-up routing via knowledge graph edges |
| `orchestrator/ask_mode.py` | Fast-path for `session_mode == "ask"` (no dispatch loop) |
| `orchestrator/state.py` | `OrchestratorState`, `ConnectorSelection`, novelty tracking |
| `orchestrator/transcript.py` | Session transcript collection for memory extraction |
| `agents/factory.py` | `create_agent()` — selects specialist agent class by connector type |

## Dependencies

- **OpenTelemetry** (`opentelemetry-sdk`) — distributed tracing
- **asyncio** — parallel fan-out via `create_task`, `asyncio.Queue`, `asyncio.timeout`
- **pydantic-ai** — LLM calls for routing and synthesis (streaming + blocking)
- `OrchestratorSkillService` — DB-resident connector skill overrides (fetched per-request)
- `UnifiedExecutor` — cached session table queries for `data_refs`

## Known issues / limitations

- Per-connector queues use non-blocking `get_nowait()` polling with a 10 ms sleep between sweeps. Under high event volume this introduces ~10 ms latency per batch. A `asyncio.Queue`-based `await get()` with per-connector tasks would eliminate the poll, but changing the queue semantics is tracked separately.
- `_decide_next_action` is still NOSONAR-tagged; its decomposition is tracked in initiative #264.

## References

- Initiative #264 — Break up top backend monster functions (specialist/orchestrator)
- Goal #255 — OSS readiness
- `docs/architecture/adding-connector.md` — how to wire a new connector into the dispatch loop
- SonarCloud rule RSPEC-3776 — cognitive complexity threshold

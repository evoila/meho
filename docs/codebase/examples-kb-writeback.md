# `examples/kb_writeback/` — closed-loop knowledge write-back

> Walkthrough of the **R3** reference pattern from Initiative
> [#807](https://github.com/evoila/meho/issues/807). Update in
> lock-step with the sample code under
> [`examples/kb_writeback/`](../../examples/kb_writeback/); stale
> entries are bugs.

## What the pattern is

An investigation agent examines a piece of operator-supplied context
(a symptom string, a fault signature, an alert that just fired). It
produces a structured finding — a writeable summary the team's
knowledge base can hold. The harness persists that finding into the
tenant kb via [`KbService`](./kb.md). A later run — a follow-up
agent, a different operator's query, or even the same agent on a
sibling symptom — pulls the finding back as ranked context through
the kb's hybrid BM25 + cosine retrieval. Investigation feeds future
investigation; the team's reasoning accumulates rather than being
re-derived every incident.

This is **composition** on top of MEHO's shipped primitives, not
new MEHO surface. The agent runtime (G11.1), the kb service (G4.1),
and the operator identity (G3.x) all ship in `main`. What
[`examples/kb_writeback/`](../../examples/kb_writeback/) adds is the
glue: the structured-output schema, the persistence helper, the
retrieval helper, and a CI exercise that proves the loop closes
against a real `pgvector` container.

The pattern is named **R3** in Initiative #807's reference-pattern
family (R1 = tiered triage, R2 = operator-approval gate, R3 = this,
R4 = local-Claude-as-triage). Each R-pattern ships as a parallel
sibling under `examples/` so a consumer can pick the bits relevant
to their harness without taking the whole catalogue.

## Why two halves, not one

The investigation and the retrieval halves run at different times,
against (usually) different operator contexts. Splitting them as two
`AgentDefinition`s means each half is independently:

- **Schedulable** — a scheduled trigger (G11.3) can fire the
  investigation against a stream of incoming alerts without an
  operator in the loop; the retrieval half runs synchronously when
  an operator asks a question, on a different cadence.
- **Identifiable** — the audit lineage records each half under its
  own `AgentDefinition.name` so a post-hoc walk of the audit trail
  knows whether a particular kb entry came from the writer or
  whether the reader merely cited it.
- **Replaceable** — a consumer who wants to keep the writer
  generic but customise the reader (or vice versa) swaps one
  definition while leaving the other untouched.

## Why the harness writes, not the model

The example deliberately persists the finding **after** the
investigation completes, in plain Python via
[`KbService.create_entry`](./kb.md#kbservice-kbserviceservicepy) —
not by giving the model an `add_to_knowledge` tool inside the loop.
Two reasons:

1. **Determinism.** The investigation's loop is bounded (default
   3 turns); making the kb write a separate post-loop step rules
   out a class of failures where the model decides to call
   `add_to_knowledge` repeatedly, or with malformed slugs, or in
   the wrong order relative to the final answer.
2. **Pedagogy.** The sample is documenting the *pattern* — the
   pre/post boundaries of one investigation cycle. Tangling the
   kb write into the toolset-resolver story (G11.1-T3 #810) makes
   the sample harder to read and harder to extend.

A consumer who wants the model itself to gate the write extends the
sample by adding a `toolset` block to the investigation definition
that includes `add_to_knowledge`, and dropping the harness-side
[`persist_finding_to_kb`](../../examples/kb_writeback/workflow.py)
call. The structured-output `Finding` becomes optional in that
shape; the model-driven path uses the MCP tool description as the
write contract.

## End-to-end flow

```
operator                                  MEHO chassis
   |                                          |
   |   symptom string                         |
   |----------------------------------------->|
   |                                          | run_investigation
   |                                          |   PydanticAgentRun.start
   |                                          |     framework loop
   |                                          |       turn 1..N
   |                                          |       final tool -> Finding
   |                                          |   returns AgentRunResult
   |                                          |
   |                                          | persist_finding_to_kb
   |                                          |   validate_slug
   |                                          |   KbService.create_entry
   |                                          |     -> documents row + embed
   |                                          |
   |                                          | retrieve_as_context
   |                                          |   KbService.search_entries
   |                                          |     -> BM25 + cosine + RRF
   |                                          |
   |   WriteBackResult                        |
   |<-----------------------------------------|
```

Three load-bearing properties the CI exercise asserts:

- The investigation's `Finding` is the framework's structured output
  — a validated Pydantic instance, not free text. The structured-
  output contract lifts the kb-slug discipline from the prompt level
  ("please format your answer as JSON") to the runtime level (the
  framework refuses to terminate the loop without a valid instance).
- The persisted `KbEntry`'s `tenant_id` matches the operator's
  `tenant_id` (the substrate enforces this; the harness asserts as
  a belt-and-suspenders check).
- A retrieval over the same tenant ranks the just-written entry in
  the top-3 hits for an evidence-derived query. Cross-tenant queries
  return nothing — the kb substrate's tenant scoping holds.

## Extending the sample

The three extension points a real consumer typically takes:

### Domain-specific investigation prompt

The shipped
[`_INVESTIGATION_SYSTEM_PROMPT`](../../examples/kb_writeback/agent_definitions.py)
is generic. Substitute one keyed on the consumer's domain
(incident triage, configuration drift detection, fleet-wide audit).
Keep the structured-output contract (`Finding`) stable so the
persistence step keeps working without rewriting.

### Richer `Finding` shape

A real consumer's finding accumulates domain-specific fields:
severity, blast radius, owning team, related ticket id, on-call
identity. Subclass or replace `Finding` and update
`build_finding_body` to render the new fields into the Markdown
body. The slug rule must still match
[`validate_slug`](./kb.md#invalidkbslugerror-kbschemaspy)'s
contract; a kb-write that fails slug validation raises before
touching the substrate.

### Deduplication / confidence gate

The shipped sample writes every finding unconditionally. Two common
gates a consumer adds:

- **Dedup search.** Call `KbService.search_entries` before
  `create_entry`. If a top hit's fused score exceeds a threshold,
  prefer extending the existing entry (read via `get_entry`,
  re-write with the merged body — the body-hash short-circuit
  means a same-slug re-write is cheap).
- **Confidence gate.** Add a `confidence: float` field to
  `Finding`; the harness only persists when confidence exceeds a
  configured floor. Findings below the floor land somewhere else
  (a triage queue, an audit row, the operator's chat).

A heavyweight gate combines both with an operator approval step —
that's the R2 pattern (Task [#1082](https://github.com/evoila/meho/issues/1082)).

## CI exercise

The sample's integration test lives at
[`backend/tests/integration/test_examples_kb_writeback.py`](../../backend/tests/integration/test_examples_kb_writeback.py).
Three tests:

| Test | What it proves |
|---|---|
| `test_closed_loop_writes_and_retrieves_finding` | The full loop closes against a real `pgvector` container: investigation produces `Finding`, harness writes to kb, retrieval ranks the just-written entry top-3 against evidence-derived terms. |
| `test_retrieval_does_not_cross_tenant_boundary` | A finding written under tenant A is invisible to tenant B's retrieval — the substrate's tenant scoping holds end to end through the sample's helpers. |
| `test_example_modules_import_cleanly` | Smoke check that runs without Docker; catches the case of the example tree being moved or renamed without the integration cluster being spun up. |

The first two tests are gated on Docker availability via the same
heuristic the sibling
[`test_kb_service_pg`](../../backend/tests/integration/test_kb_service_pg.py)
uses (`/var/run/docker.sock` exists). In the agent sandbox where
Docker is absent the tests skip with reason
`"Docker socket unavailable in this sandbox; runs in CI where
containers are provisioned."`. CI provisions Docker for the
integration lane, so the tests run there.

The agent runtime in the CI exercise wires a
`pydantic_ai.models.function.FunctionModel` whose callback emits the
expected `Finding` directly via the framework's structured-output
tool. No real LLM is called; the run is deterministic and offline.
The sample's *production* path runs the same `PydanticAgentRun`
seam against a real Anthropic / OpenAI / VCF Private AI Foundation
backend — the test stubs the model factory, not the seam.

## Why the directory is `examples/kb_writeback` and not `examples/kb-writeback`

The directory uses snake_case so Python treats it as a regular
package (a hyphenated directory cannot be `import`ed without a
custom shim). The pattern's full reference name in docs and issues
remains **R3 — closed-loop kb write-back**; on disk the package is
`kb_writeback`. The sibling R1 / R2 / R4 packages will follow the
same convention.

## References

- Issue [#1081](https://github.com/evoila/meho/issues/1081) — this task.
- Initiative [#807](https://github.com/evoila/meho/issues/807) — R1–R4 family.
- Goal [#800](https://github.com/evoila/meho/issues/800) — G11 agentic-ops runtime.
- [`docs/codebase/kb.md`](./kb.md) — the KB module's durable architecture doc.
- [`docs/codebase/agent-runtime.md`](./agent-runtime.md) — the G11.1 agent loop architecture.
- [`docs/codebase/memory.md`](./memory.md) — the sibling memory service the consumer uses for session / policy state.

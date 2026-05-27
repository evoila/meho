<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `examples/kb_writeback/` — closed-loop knowledge write-back

A runnable composition sample showing how an investigation agent's
structured findings persist to MEHO's knowledge base (G4) and are
retrieved as context by a later run. This is **R3** of the four
reference patterns shipped by Initiative
[#807 (G11.6)](https://github.com/evoila/meho/issues/807); R1 / R2 / R4
ship as parallel siblings in this `examples/` tree.

> **This is composition, not MEHO surface.** Every primitive the
> sample uses (the agent runtime, the kb service, the operator
> identity) is on `main` already. The sample documents how those
> pieces fit together; a consumer adapting the pattern to their own
> harness writes a tiny amount of glue (a system prompt, a tenant
> binding, a follow-up query) and reuses everything else.

## The loop

```
                    +-----------------------+
                    | symptom / signature   |
                    +-----------+-----------+
                                |
                                v
                    +-----------------------+
                    | INVESTIGATION_AGENT   |  PydanticAgentRun, 3-turn budget,
                    | system_prompt, run    |  structured output = Finding
                    +-----------+-----------+
                                |
                              Finding
                                |
                                v
                    +-----------------------+
                    | persist_finding_to_kb |  validate_slug + create_entry,
                    |                       |  stamps provenance metadata
                    +-----------+-----------+
                                |
                            KbEntry stored
                                |
                                v
                    +-----------------------+
                    | retrieve_as_context   |  KbService.search_entries
                    |  (next-run lookup)    |  ranks BM25 + cosine
                    +-----------+-----------+
                                |
                                v
                  list[KbEntrySearchHit] -> next agent's input
```

## The files

| File | What it does |
|---|---|
| [`agent_definitions.py`](./agent_definitions.py) | The two `AgentDefinition`s — investigation (writer) + retrieval (reader) — plus the `Finding` structured-output schema and a `build_finding_body` Markdown renderer. |
| [`workflow.py`](./workflow.py) | The three loop phases — `run_investigation`, `persist_finding_to_kb`, `retrieve_as_context` — plus a `run_closed_loop` convenience wrapper. Each phase is callable on its own. |
| [`README.md`](./README.md) | This file. |

The CI exercise that proves the loop closes against a real
`pgvector/pgvector:pg16` container lives at
[`backend/tests/integration/test_examples_kb_writeback.py`](../../backend/tests/integration/test_examples_kb_writeback.py).
It runs in MEHO's existing integration lane (no extra workflow); when
Docker is unavailable it skips with a `skipped-in-sandbox` reason that
the CI runner provisions away.

For the wider walkthrough of the pattern — when to use it, when not
to, and what extension points the sample leaves open — see
[`docs/codebase/examples-kb-writeback.md`](../../docs/codebase/examples-kb-writeback.md).
The KB primitive itself is documented in
[`docs/codebase/kb.md`](../../docs/codebase/kb.md).

## Adapting to your harness

The sample is generic on purpose. The three things a consumer
typically swaps are:

* **The investigation prompt.** `agent_definitions._INVESTIGATION_SYSTEM_PROMPT`
  is the example prose; substitute one keyed on your domain (incident
  triage, configuration drift detection, fleet-wide audit). The
  structured-output contract (`Finding` shape) is what stays stable
  so the persistence step keeps working.
* **The Finding shape.** A real consumer's finding accumulates
  domain-specific fields (severity, blast radius, owning team,
  related ticket, …). Subclass or replace `Finding` and update
  `build_finding_body` to render the new fields. The slug rule must
  still match `KbService`'s
  [`validate_slug`](../../backend/src/meho_backplane/kb/schemas.py)
  contract.
* **The persistence rule.** The sample writes every finding
  unconditionally. A real consumer may want a confidence threshold,
  a deduplication search against the existing kb (`search_entries`
  before `create_entry`), or a human approval gate (see R2 / #1082
  for that pattern).

## Why `examples/kb_writeback` rather than `examples/r3-kb-writeback`

The directory name uses snake_case so Python treats it as a regular
package (a hyphenated directory cannot be `import`ed without a
shim). The pattern's full reference name in docs and issues stays
**R3 — closed-loop kb write-back**; on disk the package is
`kb_writeback`.

## Running the sample yourself

The sample's CI lane uses a `FunctionModel` to keep the run
deterministic and offline. Pointing it at a real model is the same
shape — substitute the runtime:

```python
import asyncio
from uuid import UUID

from meho_backplane.agent import PydanticAgentRun, default_model_factory
from meho_backplane.auth.operator import Operator, TenantRole
from examples.kb_writeback.workflow import run_closed_loop


async def main() -> None:
    operator = Operator(
        sub="alice@example.com",
        name="Alice Operator",
        email=None,
        raw_jwt="<your real JWT>",  # only the operator identity bits matter
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_role=TenantRole.OPERATOR,
    )
    runtime = PydanticAgentRun(model_factory=default_model_factory)
    result = await run_closed_loop(
        operator=operator,
        symptom="vCenter 9.0 snapshot revert hangs on quiesced VMs",
        follow_up_query="vmware tools quiesce handshake snapshot",
        runtime=runtime,
    )
    print(f"finding slug={result.entry.slug} (id={result.entry.id})")
    for hit in result.retrieval_hits[:3]:
        print(f"  hit slug={hit.slug} fused_score={hit.fused_score:.2f}")


asyncio.run(main())
```

The MEHO chassis (Postgres, the embedding service, the model
provider, the operator JWT) needs to be wired up as it would be for
any in-process consumer of `KbService` + `PydanticAgentRun`. See
[`backend/README.md`](../../backend/README.md) for the local-dev
stack.

## References

- Issue [#1081](https://github.com/evoila/meho/issues/1081) — this task.
- Initiative [#807](https://github.com/evoila/meho/issues/807) — R1–R4 family.
- Goal [#800](https://github.com/evoila/meho/issues/800) — G11 agentic-ops runtime.
- [`docs/codebase/kb.md`](../../docs/codebase/kb.md) — the KB module's durable architecture doc.
- [`docs/codebase/examples-kb-writeback.md`](../../docs/codebase/examples-kb-writeback.md) — the deeper walkthrough.
- [`docs/codebase/agent-runtime.md`](../../docs/codebase/agent-runtime.md) — the G11.1 agent loop architecture.

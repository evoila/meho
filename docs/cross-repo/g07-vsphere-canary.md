<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G0.7 vSphere canary — operator procedure

This document is the **operator-facing runbook** for the G0.7
(spec-ingestion pipeline) vSphere canary. It complements the
acceptance test at
[`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py),
which automates the same flow in CI. Run this procedure when
verifying the canary against a fresh deploy, when re-running the
canary after a connector / spec rev, or when reproducing a CI
failure locally.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests the consumer's vCenter REST OpenAPI 3.0 spec
   (~1275 operations across appliance, esx, content, vcenter, hvc,
   stats, and trusted-infrastructure path families).
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   ``endpoint_descriptor`` table under the
   ``(product, version, impl_id) = ("vmware", "9.0", "vmware-rest")``
   connector triple and auto-registers a
   ``GenericRestConnector`` shim in the v2 connector registry.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives 8-15
   ``operation_group`` rows with operator-readable ``when_to_use``
   hints and per-op group assignments. Idempotent re-runs are no-ops.
4. **Review.** T4's
   [`ReviewService`](../../backend/src/meho_backplane/operations/ingest/service.py)
   exposes ``edit_group`` and ``edit_op`` for operator polish on the
   LLM output before the connector goes live.
5. **Enable.** ``enable_connector`` cascades every staged group to
   ``review_status='enabled'`` and every staged op to
   ``is_enabled=True``, surfacing them through the agent meta-tools.
6. **Search.** T8's
   [`search_operations`](../../backend/src/meho_backplane/operations/meta_tools.py)
   hybrid BM25 + pgvector cosine RRF retrieval returns the canonical
   operation in the top-3 hits for 8 of 10 representative govc-parity
   queries. Two queries currently fail; see *Known gaps* below.

## Prerequisites

- The vCenter OpenAPI spec checked out locally. The canary's spec
  resolver
  ([`tests/acceptance/_vcenter_spec.py`](../../backend/tests/acceptance/_vcenter_spec.py))
  reads, in priority order:
  - ``MEHO_VCENTER_OPENAPI_VCENTER`` — absolute path to
    ``vcenter.yaml``.
  - ``MEHO_VCENTER_OPENAPI`` — legacy path to ``vcenter.yaml``.
  - ``MEHO_CONSUMER_DOCS_ROOT`` — directory containing
    ``vcenter-9.0/vcenter.yaml``.

  The maintainer's checkout of the spec-shelf repo
  (``claude-ecp/docs/vcenter-9.0/`` in the predecessor MEHO.X
  context, or wherever the consumer keeps the vCenter spec corpus
  in your deploy) is the conventional source.

- A Postgres instance with pgvector + FTS extensions. Local
  development uses the testcontainers fixture; production uses the
  ``pgvector/pgvector:pg16``-derived chart image.

- A running backplane with ``meho connector ingest`` available
  (T5, #486). The connector CLI talks to the REST API at
  ``http(s)://<backplane>/api/v1/connectors/ingest``.

- An LLM client configured for the grouping pass. Production
  deployments wire the Anthropic Messages-API adapter under
  ``IngestionPipelineService(..., llm_client_factory=...)``; the
  canary acceptance test uses a deterministic stub (see *Test
  variant* below).

## Operator procedure

### Step 1 — ingest the spec

```bash
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec /path/to/vcenter-9.0/vcenter.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "vmware-rest-9.0",
    "inserted_count": 1275,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": true,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "vmware-rest-9.0",
    "groups_created": 8,
    "operations_assigned": 1100,
    "operations_unassigned": 175,
    "llm_call_count": 27,
    "llm_duration_ms": 45000.0
  }
}
```

Numbers approximate; the load-bearing checks are
``inserted_count >= 950``, ``8 <= groups_created <= 15``,
and ``operations_unassigned / inserted_count < 50%``.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review vmware-rest-9.0
```

Expected: a rendered table of 8-15 groups with their
``when_to_use`` hints and per-group operation counts.
Inspect each group's ``when_to_use`` for clarity — the agent reads
this verbatim to pick which group to search within.

### Step 3 — polish weak hints (optional)

```bash
meho connector edit-group vmware-rest-9.0 vm \
  --when-to-use "Use these operations for any virtual-machine workflow: list, inspect, power on/off, clone, snapshot, migrate, or otherwise manage a VM. The single largest family in the vCenter REST surface."
```

The acceptance test exercises this path against the ``vm`` group as
a smoke test; production runs may need to polish 2-4 groups
depending on the model's day-of-run output.

### Step 4 — mark per-op safety overrides for destructive verbs

```bash
meho connector edit-op vmware-rest-9.0 'DELETE:/vcenter/vm/{vm}' \
  --safety dangerous --requires-approval
```

The parser defaults DELETE to ``safety_level='dangerous'`` but
``requires_approval=false`` — operators flip the latter on any
ops whose execution should block on the approval queue.

### Step 5 — enable the connector

```bash
meho connector enable vmware-rest-9.0 --confirm
```

Cascades every staged group to ``review_status='enabled'`` and
every staged op to ``is_enabled=True``. After this step, the agent
meta-tools see the connector.

### Step 6 — smoke the agent path

```bash
meho operation groups vmware-rest-9.0
meho operation search vmware-rest-9.0 "list virtual machines" --limit 10
```

The first command should return 8-15 enabled groups. The second
should return ranked hits — the load-bearing acceptance bar is
"top-3 contains the canonical operation for the workflow". The
[`acceptance test`](../../backend/tests/acceptance/test_g07_vsphere_canary.py)
runs ten such queries and asserts the top-3 contract.

### Step 7 — verify dispatch end-to-end (optional, needs vcsim or live vCenter)

```bash
meho operation call vmware-rest-9.0 'GET:/vcenter/cluster' \
  --target rdc-vcenter --json
```

Expected: JSON-shaped response from vcsim / the live vCenter target.
This step requires a Target row pointing at a reachable vCenter
endpoint; ``vcsim`` (VMware's simulator) suffices for read
operations and is what the Initiative #389 acceptance criteria
imply for the canary's dispatch leg.

## Test variant

The CI gate at
[`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py)
runs the same procedure non-interactively against a
testcontainers Postgres + a deterministic LLM stub that classifies
ops by URL path prefix. The stub keeps the test reproducible and
fast (~5 s ingest + ~1-2 s per benchmark query); a live-LLM variant
gated on ``MEHO_G07_CANARY_LIVE_LLM=1`` is reserved for the day the
production Anthropic adapter (Task #467) lands.

The acceptance test asserts:

- ≥950 ``endpoint_descriptor`` rows persisted under the canary
  connector.
- Every persisted row carries the ``spec:vcenter.yaml`` tag.
- 8-15 ``operation_group`` rows with non-empty ``when_to_use``.
- ``review_status='enabled'`` after the enable cascade.
- One audit row written by ``edit_group``.
- ``list_operation_groups`` surfaces every enabled group with
  ``operation_count > 0``.
- ``search_operations`` returns the canonical operation in the
  top-3 for 8 of 10 govc-parity queries (the two known-failing
  queries are marked ``xfail(strict=True)`` so the suite detects
  when description quality improves enough to flip them).
- LLM call count matches ``1 + ceil(op_count / batch_size)``.
- ``search_operations`` against an unknown connector returns an
  empty hit list (not an error).

The 10 (query, expected_op_id) govc-parity pairs are:

| # | Query | Canonical op (top-3 expected) |
|---|---|---|
| 1 | `list virtual machines` | `GET:/vcenter/vm` (currently xfail — see *Known gaps*) |
| 2 | `list clusters` | `GET:/vcenter/cluster` |
| 3 | `list datacenters` | `GET:/vcenter/datacenter` |
| 4 | `list datastores` | `GET:/vcenter/datastore` |
| 5 | `list networks` | `GET:/vcenter/network` |
| 6 | `list hosts` | `GET:/vcenter/host` |
| 7 | `power on virtual machine` | `POST:/vcenter/vm/{vm}/power?action=start` (currently xfail — see *Known gaps*) |
| 8 | `power off virtual machine` | `POST:/vcenter/vm/{vm}/power?action=stop` (currently xfail — see *Known gaps*) |
| 9 | `create login session` | `POST:/session` |
| 10 | `get virtual machine info` | `GET:/vcenter/vm/{vm}` |

## Known gaps (filed as PR-body follow-ups)

### 1. `vi-json.yaml` is not yet ingested

The second vSphere spec corpus (~2195 Managed Object operations)
uses ``$ref: '#/components/parameters/moId'`` on every operation. The
T1 parser explicitly rejects non-schema component refs
(``refs.py::resolve_shallow_ref`` raises
``UnsupportedSpecError``). Extending the parser to resolve
``#/components/parameters/*`` is small (~40 LoC + tests) but
lives in T1's scope, not T8's acceptance work.

Until that lands, govc workflows that fundamentally need vi-json
ops (``govc snapshot.revert``,
``govc events``, ``govc host.evac``) are not part of the canary's
benchmark.

### 2. Cardinal-op descriptions under-rank against sub-paths

Three govc-parity queries (`list virtual machines`,
`power on virtual machine`, `power off virtual machine`) currently
return sub-paths (``GET:/vcenter/vm/{vm}/data-sets``,
``POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=connect``,
``...?action=disconnect``) in their top-3 hits instead of the
canonical short-path operation.

Two drivers:
- The vCenter spec's cardinal-op descriptions carry vendor-schema
  prose ("Vcenter.VM.FilterSpec", "Powers on a powered-off or
  suspended virtual machine") rather than natural-operator-language
  summaries.
- T3's LLM-grouping pass produces per-group hints but does **not**
  yet generate per-op ``llm_instructions`` or rewrite ``summary``.
  Both would lift retrieval quality for cardinal ops with weak
  upstream descriptions.

The acceptance test marks these three queries ``xfail``
(non-strict, because pgvector's IVFFlat approximation makes the
failure non-deterministic — the same query against the same data
can pass or fail depending on the index's probed lists). The
canary's other 7 queries plus the non-benchmark assertions verify
the substrate is healthy.

### 3. `tests/integration/conftest.py` TRUNCATE statement is stale

The integration suite's per-test reset lists only
``audit_log, documents, tenant`` — but migrations 0007 (graph_node,
graph_edge) and 0008 (broadcast_override) added more
tenant-referring tables. PG rejects the TRUNCATE with
``Table "graph_node" references "tenant"`` on local runs.

The canary's
[`tests/acceptance/conftest.py`](../../backend/tests/acceptance/conftest.py)
ships a parallel ``pg_engine`` fixture with the full TRUNCATE list
so the canary works locally without modifying the integration
conftest. The integration suite gap itself is a separate
follow-up.

## Rollback

If a canary run discovers a regression in the ingestion pipeline:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable vmware-rest-9.0 --confirm
   ```

   Cascades every group to ``review_status='disabled'`` and every
   op to ``is_enabled=False``. The agent meta-tools stop surfacing
   the connector; no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   ``meho.connector.*`` row to ``audit_log``; the trail is
   sufficient to reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   ``meho connector ingest`` again — the body-hash idempotence in
   T2 means rows whose parser output didn't change stay untouched,
   while changed rows get an updated revision. After review +
   enable, the agent path re-warms.

## References

- Task: [#408](https://github.com/evoila/meho/issues/408)
- Parent Initiative: [#389](https://github.com/evoila/meho/issues/389)
- Predecessor commits: #485 (T3), #486 (T5 CLI), #487 (T7 MCP),
  #488 (T6 REST routes).
- Downstream consumer: [#227 G3.1 vSphere
  composites](https://github.com/evoila/meho/issues/227) — depends
  on this canary's substrate readiness signal.
- Acceptance test:
  [`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py).
- Codebase doc:
  [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md)
  (the substrate-level architecture this canary verifies).

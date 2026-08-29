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

End-to-end correctness of the G0.7 ingestion pipeline driven against
the **full** v0.2 vSphere ingest (both `vcenter.yaml` and
`vi-json.yaml` under one connector triple after #501 unblocked the
parameter-ref parser branch and #503 extended this canary):

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests the consumer's vCenter REST OpenAPI 3.0 spec
   (~1,275 operations across appliance, esx, content, vcenter, hvc,
   stats, and trusted-infrastructure path families) **and** the
   vi-json Managed-Object spec (~2,195 operations across
   PerformanceManager, EventManager, VirtualMachine, HostSystem,
   ClusterComputeResource, Datastore, and the smaller VIM MO
   families). The aggregate corpus is ~3,470 operations under one
   ``connector_id="vmware-rest-9.0"``.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   ``endpoint_descriptor`` table under the
   ``(product, version, impl_id) = ("vmware", "9.0", "vmware-rest")``
   connector triple and auto-registers a
   ``GenericRestConnector`` shim in the v2 connector registry on the
   **first** spec; the second spec's call sees the existing shim and
   short-circuits (``connector_registered=False``). Every row carries
   a ``spec:<source>`` tag so operators can distinguish vcenter-sourced
   ops from vi-json-sourced ops via ``meho connector review``.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives 12-18
   ``operation_group`` rows with operator-readable ``when_to_use``
   hints and per-op group assignments. Pass-1 runs once during the
   vcenter ingest (proposing the full vcenter + vi-json taxonomy up
   front); the vi-json ingest takes the partial-regrouping path
   (Pass-1 skipped) and runs only Pass-2 against the new ops.
   Idempotent re-runs are no-ops.
4. **Review.** T4's
   [`ReviewService`](../../backend/src/meho_backplane/operations/ingest/service.py)
   exposes ``edit_group`` and ``edit_op`` for operator polish on the
   LLM output before the connector goes live.
5. **Enable.** ``enable_connector`` cascades every staged group to
   ``review_status='enabled'`` and every staged op to
   ``is_enabled=True``, surfacing them through the agent meta-tools.
6. **Search.** T8's
   [`search_operations`](../../backend/src/meho_backplane/operations/meta_tools.py)
   hybrid BM25 + pgvector cosine RRF retrieval, scoped with the
   ``group`` filter to the operation group an agent would pick
   (the sanctioned flow, architecture postulate 4), returns the
   canonical operation in the top-3 hits for 9 of 13 representative
   govc-parity queries (10 vcenter + 3 vi-json). Four queries miss
   top-3 even within their group and are xfail-documented; see
   *Known gaps* below. Raw corpus-wide ranking is deliberately not
   asserted — at the 3,470-op two-spec scale it fails intuitive
   relevance bars (evoila/meho#3006 resolved this working-as-designed;
   see *Known gaps* §2).

## Prerequisites

- The vSphere OpenAPI specs checked out locally. The canary's spec
  resolver
  ([`tests/acceptance/_vcenter_spec.py`](../../backend/tests/acceptance/_vcenter_spec.py))
  reads, in priority order:
  - ``MEHO_VCENTER_OPENAPI_VCENTER`` + ``MEHO_VCENTER_OPENAPI_VI_JSON`` —
    absolute paths to ``vcenter.yaml`` and ``vi-json.yaml``. Both
    env vars set → the canary runs the two-spec ingest.
  - ``MEHO_VCENTER_OPENAPI`` — legacy path to ``vcenter.yaml`` only.
  - ``MEHO_CONSUMER_DOCS_ROOT`` — directory containing
    ``vcenter-9.0/vcenter.yaml`` and ``vcenter-9.0/vi-json.yaml``.

  The maintainer's checkout of the spec-shelf repo
  (``claude-ecp/docs/vcenter-9.0/`` in the predecessor MEHO.X
  context, or wherever the consumer keeps the vCenter spec corpus
  in your deploy) is the conventional source.

  Single-spec fallback: when only ``vcenter.yaml`` resolves, the
  canary's two-spec assertions skip while the single-spec
  assertions still run. This preserves the existing CI matrix
  behaviour where one env var was the only requirement.

- A Postgres instance with pgvector + FTS extensions. Local
  development uses the testcontainers fixture; production uses the
  ``pgvector/pgvector:pg16``-derived chart image.

- A running backplane with ``meho connector ingest`` available
  (T5, #486). The connector CLI talks to the REST API at
  ``http(s)://<backplane>/api/v1/connectors/ingest``.

- An LLM client configured for the grouping pass. **No production
  ``LlmClient`` adapter ships in the chassis today** —
  ``set_llm_client_factory`` is the wire-up seam, but FastAPI
  lifespan startup has no caller for it, so non-dry-run ingest
  returns HTTP 503 / ``LlmClientUnavailable`` on stock deploys.
  Operators install a real adapter (Anthropic Messages-API binding
  or provider-routed via G11.5) and pass it via
  ``IngestionPipelineService(..., llm_client_factory=...)`` /
  ``set_llm_client_factory(...)`` to unblock the canary on a live
  backplane. The canary acceptance test uses a deterministic stub
  (see *Test variant* below) so it can run without an adapter
  wired. Full framing in
  [``docs/codebase/spec-ingestion.md``](../codebase/spec-ingestion.md#llm-client-wiring).

## Operator procedure

### Step 1 — ingest the specs

```bash
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec /path/to/vcenter-9.0/vcenter.yaml \
  --spec /path/to/vcenter-9.0/vi-json.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "vmware-rest-9.0",
    "inserted_count": 3470,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": true,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "vmware-rest-9.0",
    "groups_created": 14,
    "operations_assigned": 2900,
    "operations_unassigned": 570,
    "llm_call_count": 71,
    "llm_duration_ms": 110000.0
  }
}
```

Numbers approximate; the load-bearing checks are
``inserted_count >= 3200``, ``12 <= groups_created <= 18``,
and ``operations_unassigned / inserted_count < 50%``. Per-spec
``inserted_count >= 1200`` (vcenter) and ``>= 2000`` (vi-json) are
asserted by the acceptance test against the per-call
``IngestionResult``.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review vmware-rest-9.0
```

Expected: a rendered table of 12-18 groups in two-spec mode
(8-15 single-spec fallback) with their ``when_to_use`` hints
and per-group operation counts.
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
meho operation search vmware-rest-9.0 "list clusters" --group cluster --limit 10
```

The first command should return 12-18 enabled groups in two-spec
mode (8-15 single-spec fallback). The second should return ranked
hits — the load-bearing acceptance bar is "top-3 contains the
canonical operation for the workflow, searching within the group an
agent would pick from the groups listing" (the sanctioned agent
flow; raw corpus-wide search is not the acceptance bar — see
*Known gaps*). The
[`acceptance test`](../../backend/tests/acceptance/test_g07_vsphere_canary.py)
runs thirteen such queries (ten vcenter + three vi-json) and
asserts the group-scoped top-3 contract.

### Step 7 — verify dispatch end-to-end

```bash
meho operation call vmware-rest-9.0 'GET:/vcenter/cluster' \
  --target rdc-vcenter --json
```

Expected: JSON-shaped response from vcsim / the live vCenter target.
This step requires a Target row pointing at a reachable vCenter
endpoint; ``vcsim`` (VMware's simulator) suffices for read
operations and is what the Initiative #389 acceptance criteria
imply for the canary's dispatch leg.

CI now runs this step on every PR: G3.1-T8 (#515) shipped the
[`vcsim_endpoint`](../../backend/tests/acceptance/_vcsim.py)
session-scoped fixture and three acceptance modules
([`test_vmware_rest_dispatch_smoke.py`](../../backend/tests/acceptance/test_vmware_rest_dispatch_smoke.py),
[`test_vmware_rest_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_vmware_rest_jsonflux_force_handle.py),
[`test_vmware_rest_agent_flow_e2e.py`](../../backend/tests/acceptance/test_vmware_rest_agent_flow_e2e.py))
that dispatch against the simulator on every PR, on the
meho-runners-ci pool where Docker is provisioned. The
[`vcsim integration testing`](../architecture/vcsim-integration-testing.md)
doc explains the fixture pattern and how future G3.x connectors
can mirror it (Vault → testcontainers' `vault`, K8s → k3d,
bind9 → real bind9 container).

## Test variant

The CI gate at
[`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py)
runs the same procedure non-interactively against a
testcontainers Postgres + a deterministic LLM stub that classifies
ops by URL path prefix. With the CI spec shelf armed, the full
two-spec ingest (~164 s on CI runners) runs ONCE per module via the
module-scoped ``_canary_corpus`` fixture and is shared read-only by
every armed test (amortised in PR #2995 — the original per-test
ingest shape, 25 ingests, timeout-killed the 60-min integration
lane on its first armed run); a live-LLM variant
gated on ``MEHO_G07_CANARY_LIVE_LLM=1`` exercises the real grouping
pass. As of #1386 a production Anthropic ``LlmClient``
(``build_anthropic_ingest_llm_client``) is wired at FastAPI lifespan
startup, reusing ``settings.anthropic_api_key``, so the live-LLM
variant runs whenever ``ANTHROPIC_API_KEY`` is set (it skips when the
key is absent — the previously-cited ``Task #467`` was the audit CLI
verb tracker, not an LLM adapter; see
``docs/codebase/spec-ingestion.md`` §"LLM-client wiring" for the
operator-facing framing).

The acceptance test asserts:

- ≥1,200 ``endpoint_descriptor`` rows from ``vcenter.yaml`` and ≥2,000
  rows from ``vi-json.yaml`` persisted under the canary connector
  (single-spec mode requires only the vcenter floor).
- Every persisted row carries exactly one ``spec:<source>`` tag;
  vcenter and vi-json rows never share an ``op_id``.
- 12-18 ``operation_group`` rows in two-spec mode (8-15 in
  single-spec mode), each with non-empty ``when_to_use``.
- ``review_status='enabled'`` after the enable cascade.
- One audit row written by ``edit_group``.
- ``list_operation_groups`` surfaces every enabled group with
  ``operation_count >= 0``; at least eight groups carry ops, and in
  two-spec mode every group carries ops.
- Per-call ``IngestionResult.connector_registered`` is ``True`` on
  the first ingest and ``False`` on the second (auto-shim idempotency
  branch).
- ``operations_unassigned / inserted_count < 50%`` across the
  combined corpus (xfail-documented: measured 81.6% unassigned
  two-spec against the real corpus — see *Known gaps* #4).
- One vi-json ``{moId}`` path substitutes cleanly via
  :func:`meho_backplane.operations._branches._substitute_path`
  without special-casing.
- ``search_operations`` with the ``group`` filter (the sanctioned
  agent flow) returns the canonical operation in the group-scoped
  top-3 for 9 of 13 govc-parity queries (four queries that miss
  top-3 even in-group are marked ``xfail`` with their measured
  rank; vi-json queries skip in single-spec mode).
- LLM call count tracks the two-pass / multi-ingest contract:
  single-spec ≈ ``1 + 26`` calls, two-spec ≈ ``1 + 26 + 44`` calls
  (Pass-1 runs once, Pass-2 runs per spec on its unassigned ops).
- ``search_operations`` against an unknown connector returns an
  empty hit list (not an error).

The 13 (query, group, expected_op_id) govc-parity triples are —
``group`` being the operation group an agent would pick from
``list_operation_groups``, and the one ``search_operations`` is
scoped to:

| # | Query | Group | Canonical op (group-scoped top-3 expected) |
|---|---|---|---|
| 1 | `list virtual machines` | `vm` | `GET:/vcenter/vm` (xfail: in-group rank 7 measured — see *Known gaps*) |
| 2 | `list clusters` | `cluster` | `GET:/vcenter/cluster` |
| 3 | `list datacenters` | `datacenter` | `GET:/vcenter/datacenter` |
| 4 | `list datastores` | `datastore` | `GET:/vcenter/datastore` |
| 5 | `list networks` | `network` | `GET:/vcenter/network` |
| 6 | `list hosts` | `host` | `GET:/vcenter/host` |
| 7 | `power on virtual machine` | `vm` | `POST:/vcenter/vm/{vm}/power?action=start` (xfail: in-group rank 5-6 — see *Known gaps*) |
| 8 | `power off virtual machine` | `vm` | `POST:/vcenter/vm/{vm}/power?action=stop` (xfail: in-group rank 5-6 — see *Known gaps*) |
| 9 | `create login session` | `session` | `POST:/session` |
| 10 | `get virtual machine info` | `vm` | `GET:/vcenter/vm/{vm}` |
| 11 | `revert vsphere snapshot` | `vm_managed_objects` | `POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task` (vi-json) |
| 12 | `tail vsphere events` | `events` | `POST:/EventManager/{moId}/QueryEvents` (vi-json; xfail: in-group rank 4 — see *Known gaps*) |
| 13 | `get vm performance metrics` | `performance` | `POST:/PerformanceManager/{moId}/QueryPerf` (vi-json) |

The three vi-json queries (#11-#13) skip in single-spec CI matrices
where only ``MEHO_VCENTER_OPENAPI_VCENTER`` is set. Query #11's
expected op is the spec truth: in vi-json.yaml (as in the VIM API),
``RevertToSnapshot_Task`` is a method of *VirtualMachineSnapshot*,
not of *VirtualMachine* — the previously-listed
``POST:/VirtualMachine/{moId}/RevertToSnapshot_Task`` does not
exist in the corpus (VirtualMachine only carries
``RevertToCurrentSnapshot_Task``) and was latent because the armed
integration lane runs ``pytest -x`` and stopped on the
`list clusters` failure before this case ever executed. Query #12
is xfail-documented: ``QueryEvents`` measures at a stable in-group
rank 4 (the short EventManager property-reads win on BM25 text
density) — the same per-op description-quality limitation as the
vcenter cardinal ops, surfaced rather than silently absorbed.

## Opt-in extensions

Two extensions to the default canary surface, both env-gated so the
default CI run keeps skipping them cleanly. They ride existing
substrate — no production code changes shipped with these.

### Real-LLM eyeball check (`G07_CANARY_REAL_LLM=1` + `ANTHROPIC_API_KEY`)

The default canary uses a deterministic stub that classifies operations
by URL path prefix; the queries marked `xfail` in the stub
benchmark reflect the stub's inability to enrich
`when_to_use` strings beyond what the spec source carries.

The opt-in `test_g07_canary_real_llm_eyeball` test drives the same
ingestion pipeline against Claude Haiku
(`claude-haiku-4-5-20251001`) over `httpx` — no `anthropic` SDK
dependency, the chassis stays narrow. The test then asserts the
**strict top-3** govc-parity contract on the 10 vcenter queries
(the vi-json entries are excluded — this fixture ingests
`vcenter.yaml` only), naming any that miss. Unlike the stub
benchmark (group-scoped since PR #2995), the eyeball deliberately
searches **raw** — it measures exactly the raw-relevance question
evoila/meho#3006 examined (resolved working-as-designed; see *Known
gaps* §2), and it never runs in CI. This is the
parallel signal the parent Initiative's acceptance criteria call
for; the stub-path xfail markers remain unchanged.

Operator command:

```bash
export G07_CANARY_REAL_LLM=1
export ANTHROPIC_API_KEY=sk-ant-...
export MEHO_VCENTER_OPENAPI_VCENTER=/path/to/vcenter.yaml
cd backend && uv run pytest -x \
  tests/acceptance/test_g07_vsphere_canary.py::test_g07_canary_real_llm_eyeball
```

Cost note: the grouping pass issues ~27 sequential Messages-API
calls per run (1 Pass-1 propose + ~26 Pass-2 batch-assignments at
batch size 50). At Haiku 4.5 pricing this is well under USD 0.10
per run; not gated in CI by cost, gated only because the canary's
default contract is a sandbox-safe skip.

### vcsim dispatch + audit/broadcast assertion (`MEHO_VCSIM_TARGET=<base-url>`)

The default canary documents Step 7 (dispatch verification) as a
**manual** step — its acceptance suite has no `Target` row. The
opt-in `test_g07_canary_vcsim_dispatch` test automates that step:
it seeds a `Target` row matching the env value, patches the
dispatcher's already-bound `publish_event` import (the bind site
is `meho_backplane.operations._audit`, not the broadcast package
— patching the package alone is insufficient because the audit
helper resolved the symbol at import time), patches the auto-shim's
`auth_headers` to return `{}` (vcsim is no-auth), then dispatches
`GET:/vcenter/cluster` via `call_operation` and asserts:

- `result['status'] == 'ok'`
- Exactly **+1** `audit_log` row with `method='DISPATCH'` and
  `path='GET:/vcenter/cluster'`
- At least one captured `BroadcastEvent` referencing the same `op_id`

The `audit_and_broadcast_safe` codepath at
[`backend/src/meho_backplane/operations/_audit.py`](../../backend/src/meho_backplane/operations/_audit.py#L189)
thus gains an explicit canary-level assertion rather than relying
only on the per-unit tests it ships with.

Operator command:

```bash
# In one shell: start vcsim
vcsim -l :8989 &
# In another:
export MEHO_VCSIM_TARGET=http://localhost:8989
export MEHO_VCENTER_OPENAPI_VCENTER=/path/to/vcenter.yaml
cd backend && uv run pytest -x \
  tests/acceptance/test_g07_vsphere_canary.py::test_g07_canary_vcsim_dispatch
```

The `MEHO_VCSIM_TARGET` value's path component is honoured verbatim
when present; a bare host:port URL gets `/rest` appended (vcsim's
default vCenter REST mount).

## Known gaps (filed as PR-body follow-ups)

### 1. `vi-json.yaml` ingestion landed; vcenter cardinal-op gap remains

`vi-json.yaml` full ingestion landed in #503's wake — see #501
(parser extension that resolved ``$ref: '#/components/parameters/*'``)
and #503 (this canary's extension to drive the full two-spec ingest
through the production ``IngestionPipelineService``). The connector
now spans both specs (~3,470 ops under ``vmware-rest-9.0``); rows
tagged ``spec:vcenter.yaml`` vs ``spec:vi-json.yaml`` for operator
introspection via ``meho connector review``.

The govc workflows that fundamentally need vi-json ops are now
covered by benchmark queries #11-#13:

- ``govc snapshot.revert`` → ``POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task``
- ``govc events`` → ``POST:/EventManager/{moId}/QueryEvents``
- ``govc metric.sample`` → ``POST:/PerformanceManager/{moId}/QueryPerf``

Queries #11 and #13 pass group-scoped (ranks 2 and 1 measured
2026-08-17). Query #12 is xfail-documented at a stable in-group
rank 4 — a real vi-json description-quality finding (the short
EventManager property-reads out-score ``QueryEvents`` on BM25 text
density), surfaced in the suite report rather than silently
absorbed; the fix belongs to the same per-op enrichment follow-up
as the vcenter cardinal ops below.

### 2. Weak per-op descriptions under-rank inside their own group

Four govc-parity queries miss the group-scoped top-3 (both probe
samples, 2026-08-17, two-spec corpus — the group filter removes
cross-spec noise, so what remains is purely in-group ranking):

- `list virtual machines` → ``GET:/vcenter/vm`` at in-group rank 7
  (148-op `vm` group; ``GET:/vcenter/vm/{vm}/data-sets`` and
  hardware sub-paths out-rank the cardinal).
- `power on virtual machine` / `power off virtual machine` →
  ``power?action=start`` / ``?action=stop`` at in-group rank 5-6
  (hardware ``?action=connect`` / ``?action=disconnect`` sub-paths
  win).
- `tail vsphere events` → ``QueryEvents`` at in-group rank 4
  (8-op `events` group; the short property-reads ``latestEvent`` /
  ``description`` plus ``LogUserEvent`` win on BM25 text density).

Two drivers:
- The vendor specs' cardinal-op descriptions carry vendor-schema
  prose ("Vcenter.VM.FilterSpec", "Powers on a powered-off or
  suspended virtual machine") rather than natural-operator-language
  summaries.
- T3's LLM-grouping pass produces per-group hints but does **not**
  yet generate per-op ``llm_instructions`` or rewrite ``summary``.
  Both would lift retrieval quality for ops with weak upstream
  descriptions.

The acceptance test marks these four queries ``xfail`` (non-strict,
because pgvector's IVFFlat approximation can drift ranks between
runs — and the integration lane runs ``pytest -x``, where a single
variance flap would kill the lane) with the measured in-group rank
in each reason string. The canary's other 9 queries plus the
non-benchmark assertions verify the substrate is healthy.

**Resolved (evoila/meho#3006, 2026-08-29):** raw corpus-wide ranking
at multi-spec scale is **working as designed**. A live two-spec probe
(3470 ops, real bge-small embeddings) confirmed postulate 4 across
five cross-spec queries: raw search ranks the REST cardinal 3rd–7th
(or below top-8) because vi-json Managed-Object ops carrying the same
noun in their indexed ``summary``/``description`` win the BM25 arm,
while the cardinal's path — the token that matters — feeds neither
ranking signal. Group-scoped search (the sanctioned flow:
``list_operation_groups`` → ``search_operations(group=…)``) lands
every cardinal at rank 1–2, the vi-json siblings being filtered into
their ``*_managed_objects`` groups. The three raw-corpus levers
considered (spec-source weighting, exact-path-token boost, cross-spec
dedupe) would improve only the raw flow agents are told not to use;
the residual in-group cardinal misses are the *separate*
per-op-description-enrichment gap tracked here (T3 does not yet
rewrite per-op ``summary``/``llm_instructions``), which those levers
would not fix. No raw-ranking work filed.

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

### 4. Stub-taxonomy coverage vs the `< 50%` unassigned bar

Measured 2026-08-18 against the real corpus (first time the case was
reachable in a full armed run — the integration lane's ``pytest -x``
had died on `list clusters` in both prior armed runs): 2,831 of 3,470
ops end up unassigned two-spec (81.6%; per-spec: vcenter 949/1,275,
vi-json 1,882/2,195), far past the canary's ``< 50%`` acceptance bar,
which was authored from the stub taxonomy's coverage claim without an
armed measurement. The identical failure reproduces on the
pre-amortisation test shape, so this is a latent canary-calibration
gap, not a regression. The cause is structural: the deterministic
stub maps only 14 path families by design, and the real specs' long
tail (vcenter ``appliance/``/``esx/``/``hvc/``/``content/`` subtrees;
~100 vi-json ManagedObject types beyond the six mapped) classifies to
``none``. The assignment substrate itself is proven — 639 ops
populate all 14 groups and group-scoped search works against them.
``test_canary_two_spec_grouping_unassigned_ratio`` is xfail-marked
(non-strict) with the measured numbers; the fix is either a broader
stub taxonomy or a re-derived bar, both belonging to the T3
grouping-quality follow-up rather than this harness PR.

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

- Original task: [#408 G0.7-T8](https://github.com/evoila/meho/issues/408)
- Two-spec extension task: [#503 G3.1-T3 vi-json.yaml full ingestion](https://github.com/evoila/meho/issues/503)
- Parser extension task: [#501 G0.7-T11 OpenAPI parameter-ref resolver](https://github.com/evoila/meho/issues/501)
- Parent Initiative: [#389 G0.7](https://github.com/evoila/meho/issues/389) (substrate); [#227 G3.1](https://github.com/evoila/meho/issues/227) (vmware-rest-9.0 connector).
- Predecessor commits: #485 (T3), #486 (T5 CLI), #487 (T7 MCP),
  #488 (T6 REST routes), #516 (T11 parameter-ref resolver).
- Acceptance test:
  [`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py).
- Codebase doc:
  [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md)
  (the substrate-level architecture this canary verifies).

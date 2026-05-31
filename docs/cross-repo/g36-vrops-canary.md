<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.6 vROps canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
vROps 9.0 ``/suite-api`` OpenAPI surface through the G0.7 spec-
ingestion pipeline and curating the read-only v0.5 core (8 ops) the
agent surfaces through `search_operations` / `call_operation`. Run
this procedure when standing up a vROps target against a fresh
deploy, when re-running after a connector / spec revision, or when
verifying the curation against a staging vROps appliance.

Companion to
[`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md)
(the NSX counterpart this runbook mirrors) and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The vROps-specific delta is
the HTTP Basic auth model (no session token), the optional
``auth-source`` query parameter for AD / vIDM federation, and the
curated 8-op read core.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven against
the vROps suite-api spec:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests `vcf-operations-9.0/suite-api.yaml` under one
   `connector_id="vrops-rest-9.0"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   `endpoint_descriptor` table under the
   `(product, version, impl_id) = ("vcf-operations", "9.0", "vrops-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`VcfOperationsConnector`](../../backend/src/meho_backplane/connectors/vcf_operations/connector.py)
   already registered (G3.6-T1 #829); the auto-shim's idempotency
   check (`ensure_connector_class_registered`) short-circuits.
   Every row carries a `spec:vcf-operations-9.0/suite-api.yaml`
   tag so operators can audit per-spec coverage via
   `meho connector review`.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable
   `when_to_use` hints + per-op group assignments. The path-prefix
   classifier in
   [`meho_backplane.connectors.vcf_operations.core_ops.VROPS_PATH_RULES`](../../backend/src/meho_backplane/connectors/vcf_operations/core_ops.py)
   names the 7 groups the curated core spans.
4. **Curate.** The operator drives `apply_vrops_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector to
   land the 8 read-only core ops + their guidance blobs.
5. **Verify.** The operator dispatches one list op through
   `call_operation` to prove the end-to-end agent path works. The
   JSONFlux *seam* is exercised via the test-only
   `ForceHandleReducer` pattern (see acceptance tests below); real
   JSONFlux reduction is a v0.5.next concern per Goal #214 scope.

## Prerequisites

- **The vROps OpenAPI spec checked out locally.** The canary
  resolves `docs:vcf-operations-9.0/suite-api.yaml` against
  `$CLAUDE_RDC_DOCS` when set; otherwise pass a full `file:///`
  path. The spec ships in the consumer's spec-shelf repo.
- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses the
  `pgvector/pgvector:pg16`-derived chart image.
- **A running backplane with `meho connector ingest` available.**
  The connector CLI talks to the REST API at
  `http(s)://<backplane>/api/v1/connectors/ingest`.
- **An LLM client configured for the grouping pass.** **No
  production `LlmClient` adapter ships in the chassis today** —
  `set_llm_client_factory` is the wire-up seam but FastAPI
  lifespan startup has no caller for it, so non-dry-run ingest
  returns HTTP 503 / `LlmClientUnavailable` on stock deploys.
  Operators install a real adapter (Anthropic Messages-API or
  provider-routed via G11.5) and pass it via
  `IngestionPipelineService(..., llm_client_factory=...)` to
  unblock the canary on a live backplane; see
  [`docs/codebase/spec-ingestion.md` §"LLM-client wiring"](../codebase/spec-ingestion.md#llm-client-wiring)
  for the operator-facing framing.
- **A Vault path holding the vROps service-account credentials.**
  The `VcfOperationsConnector._creds` cache resolves
  `target.secret_ref` to a `{"username": ..., "password": ...}`
  pair via the
  [shared `CredentialsCache`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py)
  loader; default loader raises `NotImplementedError` until G0.3
  (#224) lands operator-context Vault reads.

## vROps auth divergence from NSX

Unlike NSX (which requires a session-cookie + XSRF-token dance),
vROps' `/suite-api/api/*` surface accepts **HTTP Basic on every
request** — no session establish call, no token refresh, no 401
retry loop. The connector caches the Vault-loaded credentials per
target and computes the `Authorization: Basic <b64>` header on each
call.

When the deployment federates identity through `vIDM` or an Active
Directory realm, set `target.auth_source` to the realm name. The
connector appends `?auth-source=<value>` as a query parameter on
every authenticated request so vROps routes the Basic challenge to
the named identity domain. When `target.auth_source` is `None` (the
default), the parameter is omitted and vROps falls back to its
local realm.

## Operator procedure

### Step 1 — ingest the spec

```bash
meho connector ingest \
  --product vcf-operations --version 9.0 --impl vrops-rest \
  --spec docs:vcf-operations-9.0/suite-api.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "vrops-rest-9.0",
    "inserted_count": 700,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "vrops-rest-9.0",
    "groups_created": 10,
    "operations_assigned": 600,
    "operations_unassigned": 100,
    "llm_call_count": 15,
    "llm_duration_ms": 25000.0
  }
}
```

Numbers are approximate; the load-bearing checks are
`inserted_count >= 500`, `7 <= groups_created <= 14`, and
`operations_unassigned / inserted_count < 50%`.
`connector_registered` is `false` here because
`VcfOperationsConnector` is already registered in the v2 registry
at module-import time (G3.6-T1) — the auto-shim finds the existing
entry and short-circuits.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review vrops-rest-9.0
```

Expected: a rendered table of 7-14 groups (the path-prefix
classifier maps to 7 named groups; the LLM may propose extras for
tails the classifier doesn't catch). Compare against the canonical
7 groups in
[`VROPS_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/vcf_operations/core_ops.py):

| group_key | name | covers |
|---|---|---|
| `vrops-system` | vROps (system / about) | `/suite-api/api/versions` |
| `vrops-resources` | vROps Resources | `/suite-api/api/resources` |
| `vrops-alerts` | vROps Alerts | `/suite-api/api/alerts` |
| `vrops-alert-definitions` | vROps Alert Definitions | `/suite-api/api/alertdefinitions` |
| `vrops-symptoms` | vROps Symptoms | `/suite-api/api/symptoms` |
| `vrops-recommendations` | vROps Recommendations | `/suite-api/api/recommendations` |
| `vrops-supermetrics` | vROps Super Metrics | `/suite-api/api/supermetrics` |

Note the rule ordering: ``/alertdefinitions`` precedes ``/alerts``
in :data:`VROPS_PATH_RULES` because ``startswith("/suite-api/api/alerts")``
would otherwise eat the longer path. The classifier's loop terminates
at the first matching prefix.

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_vrops_core_curation`. From a
backplane Python shell:

```python
from meho_backplane.connectors.vcf_operations import apply_vrops_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator=operator)
await apply_vrops_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `VROPS_CORE_GROUPS`:

1. For every non-curated op in the curated group,
   `ReviewService.edit_op(op_id, is_enabled=False)` — writes the
   operator-override audit row that the subsequent `enable_group`
   cascade respects (so non-core ops stay disabled even though
   their group is being enabled).
2. `ReviewService.edit_group(group_key, name=…, when_to_use=…)` —
   lands the operator-reviewed text the agent reads through
   `list_operation_groups`.
3. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to
   `is_enabled=True` **except** for the ops flagged in step 1.

Then for every entry in `VROPS_CORE_OPS`:

4. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands the
   per-op JSON blob the agent inlines into reasoning context when
   the op surfaces in `search_operations` hits.

Each op's `llm_instructions` is a three-key blob
(`when_to_call` / `output_shape` / `next_step`) matching the
typed-connector convention from
[`connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
and [`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py).
The same agent reads both surfaces, so the structure stays uniform
across typed and ingested connectors.

### Step 4 — verify the curation

```bash
meho operation groups vrops-rest-9.0
meho operation search vrops-rest-9.0 "list vrops alerts" --limit 5
meho operation search vrops-rest-9.0 "what vrops symptoms are active" --limit 5
```

The first command should return 7 enabled groups, each with the
canonical `when_to_use` from `VROPS_CORE_GROUPS`. The second should
return `GET:/suite-api/api/alerts` in the top-3. The third should
return `GET:/suite-api/api/symptoms` in the top-3.

Every other vROps op the spec ingestion produced should be in the
`is_enabled=False` state — `search_operations` filters on
`OperationGroup.review_status='enabled'` AND
`EndpointDescriptor.is_enabled=True`, so a staged group's ops
never surface.

### Step 5 — dispatch one list op end-to-end

Against a real probed vROps target:

```bash
meho operation call vrops-rest-9.0 \
  'GET:/suite-api/api/resources' \
  --target vrops-canary --json
```

Expected: JSON-shaped response carrying a `resourceList` array of
resource entries. The HTTP Basic header is computed from the
Vault-loaded credentials on the first dispatch and reused for
subsequent ones via the per-target `CredentialsCache`.

The JSONFlux *seam* (would-be handle threading) is exercised
non-interactively at
[`backend/tests/acceptance/test_g36_vrops_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g36_vrops_jsonflux_force_handle.py):
the test installs a `ForceHandleReducer` that always wraps the
vROps list payload in a synthetic `ResultHandle` and asserts the
dispatcher's `OperationResult.handle` carries it through. Real
JSONFlux reduction (set-shaped payload reduction, MinIO/S3 spill,
`result_query` meta-tool) is **out of scope for v0.5** per Goal
#214 — the seam test proves the dispatcher is ready for it once
the production reducer ships.

## Write-ops stay staged

vROps' write surface (custom-group create / maintenance-mode set /
alert-acknowledge) is **explicitly left disabled** in v0.5 per the
Initiative #369 out-of-scope list. The curation helper's "enable
group but pin non-core ops disabled" pattern keeps those rows on
the connector for future enablement without exposing them to the
agent until an explicit follow-up Task lands their
`llm_instructions` + safety review.

If `meho operation search vrops-rest-9.0 "<query>"` surfaces a
`POST` / `DELETE` / `PUT` op after this canary completes, the
curation has regressed — re-run `apply_vrops_core_curation` and
file an issue against Initiative #369.

## Rollback

If a canary run discovers a regression in the ingestion pipeline
or the curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable vrops-rest-9.0 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every
   op to `is_enabled=False`. The agent meta-tools stop surfacing
   the connector; no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is sufficient
   to reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in T2
   means rows whose parser output didn't change stay untouched,
   while changed rows get an updated revision. After re-curation +
   enable, the agent path re-warms.

## Known gaps

### 1. Env-gated automated canary is a follow-up

G3.6-T2 ships the operator-review substrate
(`apply_vrops_core_curation` helper), the curated 8-op data
(`VROPS_CORE_OPS`), the dispatch smoke + JSONFlux force-handle
acceptance tests over respx-mocked vROps, and this runbook. The
env-gated automated canary that mirrors `test_g07_vsphere_canary.py`
for vROps — drives the full suite-api ingest through
`IngestionPipelineService` against a real LLM stub or live
Anthropic adapter — is a follow-up. It requires the vROps spec
files reachable from CI, the same env-gated pattern
`tests/acceptance/_vcenter_spec.py` codifies for vSphere.

### 2. Goal #214 G3.6 vROps checklist line

The Goal body's `[ ] #369 — G3.6 tier-3 VCF management plane`
checkbox flips when the Initiative's whole DoD is met. G3.6-T2
moves the Initiative forward but does not on its own complete it
(G3.6-T3 #837 wires the CLI verbs + recorded-fixture E2E + the
onboarding doc). The checklist tick lives on the Initiative-level
wrap-up, not on this Task.

### 3. CLI verbs for per-op `llm_instructions` editing

`ReviewService.edit_op(llm_instructions=…)` is exposed at the
Python level (the substrate the curation helper drives). The CLI
verb `meho connector edit-op --llm-instructions <json>` and the
matching REST / MCP route extensions are deferred to G3.6-T3 #837
alongside the CLI verbs for vROps-specific operations.

## References

- Issue: [G3.6-T2 #833](https://github.com/evoila/meho/issues/833).
- Parent Initiative: [G3.6 #369](https://github.com/evoila/meho/issues/369).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.6-T1 #829](https://github.com/evoila/meho/issues/829)
  — `VcfOperationsConnector` skeleton.
- NSX counterpart: [`g35-nsx-canary.md`](./g35-nsx-canary.md).
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- vROps suite-api docs (Broadcom developer portal):
  https://developer.broadcom.com/xapis/vrealize-operations-manager-api/latest/
- Curated data:
  [`backend/src/meho_backplane/connectors/vcf_operations/core_ops.py`](../../backend/src/meho_backplane/connectors/vcf_operations/core_ops.py).
- Consumer wrapper this contract retires (per Initiative #369):
  `scripts/vcf-operations.sh` in the consumer's `claude-rdc-hetzner-dc`
  repository.

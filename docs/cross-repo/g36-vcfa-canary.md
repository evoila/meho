<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.6 VCF Automation canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
VCF Automation 9.0 dual-plane OpenAPI surface through the G0.7
spec-ingestion pipeline and curating the read-only v0.5 core
(11 ops: 6 provider + 5 tenant) the agent surfaces through
`search_operations` / `call_operation`. Run this procedure when
standing up a VCFA target against a fresh deploy, when re-running
after a connector / spec revision, or when verifying the curation
against a staging VCFA appliance.

Companion to
[`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md)
(the NSX dual-spec counterpart this runbook mirrors),
[`docs/cross-repo/g07-vsphere-canary.md`](./g07-vsphere-canary.md)
(the original vSphere two-spec canary), and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The VCFA-specific delta
is the **dual-plane** ingest layout (`cloudapi.yaml` for the
provider plane + `iaas.yaml` for the tenant plane), the per-plane
auth divergence the connector skeleton already handles (G3.6-T10
#832), and the 11-op v0.5 read core distributed across both planes.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven
against the **two-spec dual-plane VCFA corpus**:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests both `vcf-automation-9.0/cloudapi.yaml` (the
   vCloud-Director-derived provider plane covering `/cloudapi/1.0.0/*`
   and the classic `/api/*` family — orgs, regions, supervisors, IP
   spaces, edge clusters, content libraries, users) and
   `vcf-automation-9.0/iaas.yaml` (the Aria-IaaS-derived tenant
   plane covering `/iaas/api/*` — blueprints, catalog deployments,
   projects, machines, networks, load balancers) under one
   `connector_id="vcfa-rest-9.0"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   `endpoint_descriptor` table under the
   `(product, version, impl_id) = ("vcfa", "9.0", "vcfa-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`VcfAutomationConnector`](../../backend/src/meho_backplane/connectors/vcf_automation/connector.py)
   already registered (G3.6-T10 #832); the auto-shim's idempotency
   check (`ensure_connector_class_registered`) short-circuits.
   **Every row carries a `spec:<source>` tag** (`spec:cloudapi` or
   `spec:iaas`) so operators can distinguish provider-plane rows
   from tenant-plane rows via `meho connector review` and so the
   dispatcher's plane-aware auth path picks the right token at
   runtime.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable
   `when_to_use` hints. The path-prefix classifier in
   [`meho_backplane.connectors.vcf_automation.core_ops.VCFA_PATH_RULES`](../../backend/src/meho_backplane/connectors/vcf_automation/core_ops.py)
   names the 8 groups the curated core spans (4 provider + 4
   tenant).
4. **Curate.** The operator drives `apply_vcfa_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector
   to land the 11 read-only core ops + their guidance blobs.
   Every enabled group's `when_to_use` names its plane explicitly
   so the agent's grouping step routes correctly across the
   dual-plane surface.
5. **Verify.** The operator dispatches one provider-plane and one
   tenant-plane list op through `call_operation` to prove both
   auth planes work end-to-end. The deployment-list op is the
   largest tenant payload; the dispatcher's JSONFlux seam wraps
   oversized responses in a `ResultHandle` via the default
   [`JsonFluxReducer`](../architecture/jsonflux.md) (G0.6.1, #750).

## Prerequisites

- **The VCFA OpenAPI specs checked out locally.** Two specs are
  required for the dual-plane ingest; both come from the consumer's
  spec-shelf repo. Set:
  - `MEHO_VCFA_OPENAPI_CLOUDAPI` + `MEHO_VCFA_OPENAPI_IAAS` —
    absolute paths to `vcf-automation-9.0/cloudapi.yaml` and
    `vcf-automation-9.0/iaas.yaml`. Both env vars set → the
    canary drives the two-spec ingest.
  - `MEHO_CONSUMER_DOCS_ROOT` — directory containing
    `vcf-automation-9.0/cloudapi.yaml` and
    `vcf-automation-9.0/iaas.yaml`. The consumer's spec-shelf repo
    is the conventional source. Both files can be exported from a
    live VCFA 9.0 appliance via the in-product API Documentation
    portal (the provider-side "API Documentation" link from the
    System Organization dropdown surfaces the cloudapi spec; the
    tenant-side "API Help Center" link surfaces the iaas spec).

- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses
  the `pgvector/pgvector:pg16`-derived chart image.

- **A running backplane with `meho connector ingest` available**
  (T5, #486). The connector CLI talks to the REST API at
  `http(s)://<backplane>/api/v1/connectors/ingest`.

- **An LLM client configured for the grouping pass.** Production
  deploys wire the Anthropic Messages-API adapter under
  `IngestionPipelineService(..., llm_client_factory=...)`.

- **A Vault path holding the VCFA service-account credentials.**
  The `VcfAutomationConnector.credentials_loader` resolves
  `target.secret_ref` and (optionally) `target.provider_secret_ref`
  to two independent `{"username": ..., "password": ...}` pairs —
  one per auth plane (provider-plane Basic-auth POST against
  `/cloudapi/1.0.0/sessions/provider`, tenant-plane JSON-body POST
  against `/iaas/api/login`). Default loader raises
  `NotImplementedError` until G0.3 (#224) lands operator-context
  Vault reads; production deploys pre-G0.3 inject a loader at
  construction.

## VCFA dual-plane auth divergence

Unlike NSX (single auth plane, session-cookie + XSRF) or Harbor
(single auth plane, Basic), VCFA 9.0 splits its surface across two
independent auth planes on the same appliance:

| Plane | Path family | Login flow | Token carrier |
|---|---|---|---|
| Provider | `/cloudapi/*` + classic `/api/*` | `POST /cloudapi/1.0.0/sessions/provider` with HTTP Basic | `X-VMWARE-VCLOUD-ACCESS-TOKEN` response header (JWT) |
| Tenant | `/iaas/api/*` | `POST /iaas/api/login` with JSON body `{"username":…, "password":…, "domain":…}` | response body `{"token": "…"}` |

The provider JWT and the tenant token are **independent identity
domains**: the provider JWT does NOT authenticate the tenant plane
and vice versa. The connector caches both tokens per target, each
under its own `asyncio.Lock`, and selects the right token at
request time via
[`plane_for_path`](../../backend/src/meho_backplane/connectors/vcf_automation/_routing.py):
paths starting with `/iaas/api/` route to the tenant token;
everything else routes to the provider JWT. On HTTP 401 from
either plane, the connector invalidates that plane's cached token
and retries once — same posture as the NSX session-retry contract
(re-login once, never loop).

The `spec_source` tag the ingest writes onto each row
(`spec:cloudapi` for provider-plane rows, `spec:iaas` for
tenant-plane rows) is the operator-visible signal that documents
which plane an op lives on; the dispatcher's plane selection is
path-prefix-driven and doesn't read the tag at runtime, but
operators rely on it for `meho connector review` filtering and the
ai_engineering pack's "plane-aware group hints" discipline reads
the tag transitively via the group's curated `when_to_use`.

This flow is fully exercised by the `VcfAutomationConnector`
skeleton (G3.6-T10 #832); the auth-shape acceptance tests
([`test_connectors_vcf_automation_auth.py`](../../backend/tests/test_connectors_vcf_automation_auth.py))
prove both plane handshakes work against a respx-mocked VCFA
appliance.

## Operator procedure

### Step 1 — ingest both specs

```bash
meho connector ingest \
  --product vcfa --version 9.0 --impl vcfa-rest \
  --spec /path/to/vcf-automation-9.0/cloudapi.yaml \
  --spec /path/to/vcf-automation-9.0/iaas.yaml \
  --json
```

Or using the `docs:<connector-id>/<file>` shorthand the CLI
resolves against `$CLAUDE_RDC_DOCS`:

```bash
meho connector ingest \
  --product vcfa --version 9.0 --impl vcfa-rest \
  --spec docs:vcf-automation-9.0/cloudapi.yaml \
  --spec docs:vcf-automation-9.0/iaas.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "vcfa-rest-9.0",
    "inserted_count": 2400,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "vcfa-rest-9.0",
    "groups_created": 14,
    "operations_assigned": 2100,
    "operations_unassigned": 300,
    "llm_call_count": 50,
    "llm_duration_ms": 90000.0
  }
}
```

Numbers are approximate; the load-bearing checks are
`inserted_count >= 2000`, `8 <= groups_created <= 20`, and
`operations_unassigned / inserted_count < 50%`.
`connector_registered` is `false` because `VcfAutomationConnector`
is already registered in the v2 registry at module-import time
(G3.6-T10) — the auto-shim finds the existing entry on the first
spec and short-circuits on the second.

Body-hash idempotence is the key dual-spec invariant: the second
spec's call against `register_ingested_operations` upserts only
the iaas-plane ops; the cloudapi-plane ops from the first call
are matched by op_id and skipped via body-hash equality. A
follow-up ingest of the **same** specs is a no-op (both
inserted_count and updated_count = 0).

### Step 2 — review the LLM-summarised groups

```bash
meho connector review vcfa-rest-9.0
```

Expected: a rendered table of 8-20 groups. The path-prefix
classifier maps to 8 named groups; the LLM may propose extras for
tails the classifier doesn't catch (e.g. `/cloudapi/1.0.0/cells`,
`/iaas/api/machines`). The canonical 8 groups in
[`VCFA_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/vcf_automation/core_ops.py):

| group_key | plane | covers |
|---|---|---|
| `provider-site` | provider | `/cloudapi/1.0.0/site` |
| `provider-orgs` | provider | `/cloudapi/1.0.0/orgs`, `/cloudapi/1.0.0/orgs/{id}` |
| `provider-regions` | provider | `/cloudapi/1.0.0/regions`, `/cloudapi/1.0.0/regions/{id}` |
| `provider-users` | provider | `/cloudapi/1.0.0/users` |
| `tenant-about` | tenant | `/iaas/api/about` |
| `tenant-projects` | tenant | `/iaas/api/projects` |
| `tenant-deployments` | tenant | `/iaas/api/deployments`, `/iaas/api/deployments/{id}` |
| `tenant-blueprints` | tenant | `/iaas/api/blueprints` |

Operators can filter the review output by `spec_source` tag to
audit one plane at a time — `meho connector review vcfa-rest-9.0
--tag spec:cloudapi` shows only the provider-plane rows, and
`--tag spec:iaas` shows only the tenant-plane rows.

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_vcfa_core_curation`. From a
backplane Python shell:

```python
from meho_backplane.connectors.vcf_automation import apply_vcfa_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_vcfa_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `VCFA_CORE_GROUPS`:

1. `ReviewService.edit_group(group_key, name=…, when_to_use=…)`
   — lands the operator-reviewed text the agent reads through
   `list_operation_groups`. **Every curated `when_to_use` names
   its plane explicitly** (e.g. "Use this group on the VCFA
   **provider plane** to list or inspect organizations…"); the
   plane is load-bearing for correct agent routing across the
   dual-plane surface.
2. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to
   `is_enabled=True` while honouring the audit-log-driven
   operator-override exclusion for non-core ops.

Then for every entry in `VCFA_CORE_OPS`:

3. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands
   the per-op JSON blob the agent inlines into reasoning context
   when the op surfaces in `search_operations` hits.

Each op's `llm_instructions` is a three-key blob
(`when_to_call` / `output_shape` / `next_step`) matching the
typed-connector convention from
[`connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
and the prior core-ops modules
([`nsx/core_ops.py`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py),
[`harbor/core_ops.py`](../../backend/src/meho_backplane/connectors/harbor/core_ops.py)).
The same agent reads both surfaces, so the structure stays uniform
across typed and ingested connectors.

### Step 4 — verify the curation

```bash
meho operation groups vcfa-rest-9.0
meho operation search vcfa-rest-9.0 "list vcfa organizations" --limit 5
meho operation search vcfa-rest-9.0 "list tenant catalog deployments" --limit 5
meho operation search vcfa-rest-9.0 "what blueprints are available in this tenant" --limit 5
```

The first command should return 8 enabled groups, each with the
canonical `when_to_use` from `VCFA_CORE_GROUPS` (4 provider + 4
tenant). The second should return `GET:/cloudapi/1.0.0/orgs` in
the top-3 under the `provider-orgs` group. The third should
return `GET:/iaas/api/deployments` in the top-3 under the
`tenant-deployments` group — the question's "tenant catalog"
phrasing routes it onto the tenant plane via the plane-named
`when_to_use`. The fourth should return `GET:/iaas/api/blueprints`
in the top-3.

Every other VCFA op the spec ingestion produced should be in the
`is_enabled=False` state — `search_operations` filters on
`OperationGroup.review_status='enabled'` AND
`EndpointDescriptor.is_enabled=True`, so a staged group's ops
never surface.

### Step 5 — dispatch one op per plane end-to-end

Against a real probed VCFA target:

```bash
# Provider plane — list orgs.
meho operation call vcfa-rest-9.0 \
  'GET:/cloudapi/1.0.0/orgs' \
  --target vcfa-canary --json

# Tenant plane — list deployments.
meho operation call vcfa-rest-9.0 \
  'GET:/iaas/api/deployments' \
  --target vcfa-canary --json
```

Expected:

* **Provider call** — JSON-shaped response carrying a `values`
  array of organization entries (`id`, `name`, `displayName`,
  `orgVdcCount`). The provider Basic-auth login runs implicitly on
  first dispatch; the per-target provider-plane JWT cache reuses
  the token for follow-up provider-plane calls.

* **Tenant call** — JSON-shaped response carrying a `content`
  array of deployment entries. The tenant JSON-body login runs
  implicitly on first dispatch (independently of the provider
  login); the per-target tenant-plane token cache reuses the token
  for follow-up tenant-plane calls. **Large tenants** (hundreds of
  deployments) trip the JSONFlux seam: the `OperationResult.handle`
  field carries a `ResultHandle` and `result_describe` /
  `result_query` resolve against it.

The JSONFlux *seam* (would-be handle threading) is exercised
non-interactively at the same locations the NSX precedent
establishes: a `ForceHandleReducer` acceptance test installs a
synthetic reducer that always wraps the list payload in a
`ResultHandle`, then dispatches the deployment-list op against
the seeded VCFA core and asserts the dispatcher's
`OperationResult.handle` carries the synthetic handle through.
Real JSONFlux reduction (set-shaped payload reduction, MinIO/S3
spill, `result_query` meta-tool over the spilled set) is
**out of scope for v0.5** per Goal #214 — the seam test proves
the dispatcher is ready for it once the production reducer ships.

## Rollback

If a canary run discovers a regression in the ingestion pipeline
or the curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable vcfa-rest-9.0 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every
   op to `is_enabled=False`. The agent meta-tools stop surfacing
   the connector; no in-flight dispatches will use it. The
   per-target JWT and tenant-token caches on
   `VcfAutomationConnector` are not affected — they're cleared
   on lifespan shutdown only — but with the connector disabled
   the dispatcher refuses to route any op against the triple.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is
   sufficient to reconstruct what happened. Provider-plane and
   tenant-plane audit rows are not differentiated at the
   `audit_log.path` level — both share the same
   `meho.connector.edit_*` paths — but each row's `payload`
   carries the op_id, and the `spec:cloudapi` / `spec:iaas` tag
   on the referenced `endpoint_descriptor` row identifies the
   plane.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in
   T2 means rows whose parser output didn't change stay
   untouched, while changed rows get an updated revision. After
   re-curation + enable, the agent path re-warms.

## Known gaps

### 1. Env-gated automated two-spec canary is a follow-up

This Task (G3.6-T11, #836) ships the operator-review substrate
(`apply_vcfa_core_curation` helper, plane-aware `VCFA_CORE_GROUPS`
with named planes in every `when_to_use`), the curated 11-op data
(`VCFA_CORE_OPS`), and this runbook. The env-gated automated
canary that mirrors `test_g07_vsphere_canary.py` for VCFA —
drives the full two-spec ingest of `cloudapi.yaml` + `iaas.yaml`
through `IngestionPipelineService` against a real LLM stub or
live Anthropic adapter — is a follow-up. It requires the VCFA
spec files reachable from CI, the same env-gated pattern
`tests/acceptance/_vcenter_spec.py` codifies for vSphere.

### 2. Provider-plane VDC vs Region terminology

The Task body uses `vcfa.provider.vdc.list` / `vcfa.provider.vdc.get`
as the canonical op_id labels for the provider-plane compute
inventory surface. The actual VCFA 9.0 API path is
`/cloudapi/1.0.0/regions` — the VCFA 9 evolution of the legacy
vCloud Director provider VDC concept (each region groups compute,
memory, and networking under one NSX domain, typically backed by
one or more VCF workload domains). The operator-facing label is
"VCFA Provider Regions"; the agent-facing `llm_instructions`
notes the heritage so cross-references with legacy vCD-era
documentation resolve.

### 3. Tenant write ops are staged, not enabled

Per the parent Initiative #369 definition of done, write-path
tenant ops (`POST /iaas/api/deployments`, `DELETE
/iaas/api/deployments/{id}`, `POST /iaas/api/blueprints`, blueprint
publish) are ingested into the `endpoint_descriptor` table but
stay `is_enabled=False`. Operator-review can promote individual
write ops to `enabled` for specific tenants once a write-path
audit and confirmation flow lands; the v0.5 ship is strictly read.

### 4. Goal #214 G3.6 VCFA checklist line

The Goal body's `[ ] #369 — G3.6 tier-3 batch — VCFA dual-plane`
checkbox flips when the Initiative's whole DoD is met. This Task
moves the Initiative forward but does not on its own complete it
(G3.6-T12 #840 wires the CLI verbs + recorded-fixture E2E). The
checklist tick lives on the Initiative-level wrap-up, not on this
Task.

### 5. CLI verbs for per-op `llm_instructions` editing

`ReviewService.edit_op(llm_instructions=…)` is exposed at the
Python level (the substrate the curation helper drives). The CLI
verb `meho connector edit-op --llm-instructions <json>` and the
matching REST / MCP route extensions are deferred to G3.6-T12 #840
alongside the CLI verbs for VCFA-specific operations.

## References

- Issue: [G3.6-T11 #836](https://github.com/evoila/meho/issues/836).
- Parent Initiative: [G3.6 #369](https://github.com/evoila/meho/issues/369).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.6-T10 #832](https://github.com/evoila/meho/issues/832)
  — `VcfAutomationConnector` dual-plane skeleton.
- NSX dual-spec counterpart: [`g35-nsx-canary.md`](./g35-nsx-canary.md).
- vSphere dual-spec precedent: [`g07-vsphere-canary.md`](./g07-vsphere-canary.md).
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- VCF Automation API documentation portal:
  https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/about-the-vcf-automation-api.html
- Aria-IaaS-derived tenant-plane API reference:
  https://developer.broadcom.com/xapis/vm-apps-org-provisioning-service/latest/
- Curated data:
  [`backend/src/meho_backplane/connectors/vcf_automation/core_ops.py`](../../backend/src/meho_backplane/connectors/vcf_automation/core_ops.py).
- Codebase doc: [`docs/codebase/connectors-vcf-automation.md`](../codebase/connectors-vcf-automation.md).
- Consumer wrapper this contract retires (per Initiative #369):
  `scripts/vcf-automation.sh` in the consumer's `claude-rdc-hetzner-dc`
  repository (private to the `evoila-bosnia` org).

<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.6 Fleet canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
VCF Fleet (vRSLCM-derived) LCM REST OpenAPI surface through the G0.7
spec-ingestion pipeline and curating the read-only v0.5 core (8 ops)
the agent surfaces through `search_operations` / `call_operation`.
Run this procedure when standing up a Fleet target against a fresh
deploy, when re-running after a connector / spec revision, or when
verifying the curation against a staging Fleet appliance.

Companion to
[`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md) (the NSX
counterpart this runbook mirrors) and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The Fleet-specific delta is
the HTTP Basic auth flow (no SSO federation, no session token, no
XSRF dance), the **broken-in-9.0** ``/about`` known issue, and the
curated 8-op read core.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven against
the **vRSLCM LCM REST corpus**:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests `vcf-fleet-9.0/<lcm-api>.yaml` (the vRSLCM LCM REST
   OpenAPI surface; vRSLCM 1.3.0) under one
   `connector_id="fleet-rest-9.0"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   `endpoint_descriptor` table under the
   `(product, version, impl_id) = ("vcf-fleet", "9.0", "fleet-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`VcfFleetConnector`](../../backend/src/meho_backplane/connectors/vcf_fleet/connector.py)
   already registered (G3.6-T7 #831); the auto-shim's idempotency
   check (`ensure_connector_class_registered`) short-circuits.
   Every row carries a `spec:<source>` tag so operators can
   distinguish the LCM source spec via `meho connector review`.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable
   `when_to_use` hints + per-op group assignments. The
   path-prefix classifier in
   [`meho_backplane.connectors.vcf_fleet.core_ops.FLEET_PATH_RULES`](../../backend/src/meho_backplane/connectors/vcf_fleet/core_ops.py)
   names the 6 groups the curated core spans.
4. **Curate.** The operator drives `apply_fleet_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector to
   land the 8 read-only core ops + their guidance blobs.
5. **Verify.** The operator dispatches one list op through
   `call_operation` to prove the end-to-end agent path works. The
   JSONFlux *seam* (set-shaped payload handle threading) is
   exercised via the substrate-level dispatch tests; real JSONFlux
   reduction is a v0.2.next concern per Goal #214 scope.

## Prerequisites

- **The Fleet LCM OpenAPI spec checked out locally.** The canary's
  spec resolver pattern mirrors the NSX and SDDC Manager canaries.
  Set:
  - `MEHO_CONSUMER_DOCS_ROOT` — directory containing
    `vcf-fleet-9.0/<lcm-api>.yaml`. The consumer's spec-shelf repo
    is the conventional source. Or pass the absolute path directly
    to the `--spec` flag.

  The env-gated **automated** canary acceptance test that drives the
  ingest against `IngestionPipelineService` is a follow-up; until it
  lands, the operator runs this procedure manually, and the
  substrate-level curation tests live at
  [`backend/tests/test_connectors_vcf_fleet_core_ops.py`](../../backend/tests/test_connectors_vcf_fleet_core_ops.py)
  (classifier rules, read-only invariants, and the SQLite-backed
  `apply_fleet_core_curation` integration suite).

- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses the
  `pgvector/pgvector:pg16`-derived chart image.
- **A running backplane with `meho connector ingest` available**
  (T5, #486). The connector CLI talks to the REST API at
  `http(s)://<backplane>/api/v1/connectors/ingest`.
- **An LLM client configured for the grouping pass.** Production
  deploys wire the Anthropic Messages-API adapter under
  `IngestionPipelineService(..., llm_client_factory=...)`.
- **A Vault path holding the Fleet service-account credentials.**
  The `VcfFleetConnector` resolves
  `target.secret_ref` to a `{"username": ..., "password": ...}`
  pair sent as HTTP Basic on every request. The typical Fleet
  account is `admin@local` — the `@local` suffix is part of the
  literal username, not a realm decoration; Fleet has no SSO
  federation in v0.2.

## Fleet auth divergence from sister G3.6 connectors

Unlike vROps (Basic with optional `auth-source` query param) or vRLI
(session-cookie + token), Fleet uses **HTTP Basic on every request
against a local user store** (`admin@local`). There is no session
establish, no token cache, no XSRF dance. The connector keeps the
shared `CredentialsCache` (load-once-per-target from Vault) and feeds
the cached values into a per-request `Authorization: Basic <b64>`
header via `_shared/vcf_auth.basic_auth_header` (#841).

This flow is fully exercised by the `VcfFleetConnector` skeleton
G3.6-T7 #831 landed; the auth tests live at
[`backend/tests/test_connectors_vcf_fleet_auth.py`](../../backend/tests/test_connectors_vcf_fleet_auth.py).

## Fleet known-issue: `/about` returns HTTP 500 in 9.0

Fleet's first-party diagnostic endpoints (`/lcm/lcops/api/v2/about`,
`/lcm/lcops/api/v2/health`, `/lcm/lcops/api/v2/version`, and the
other entries in
[`_FLEET_BROKEN_DIAGNOSTIC_ENDPOINTS`](../../backend/src/meho_backplane/connectors/vcf_fleet/connector.py))
**return HTTP 500 in VCF 9.0 builds** — known appliance issue,
documented in the consumer wrapper. The connector's `fingerprint`
works around this by calling `GET /lcm/lcops/api/v2/datacenters` and
reading the `Lcm-API-Version` response header.

The curated `fleet.about` op is **still listed** in the read core
for parity with the spec, but its `llm_instructions.next_step` tells
the agent to fall back to `fleet.datacenter.list` on a 500. Operators
needing the product version cross-source it from SDDC Manager's
`/v1/vcf-services` LCM service entry — that's an operator-context
concern above the per-product connector.

## Operator procedure

### Step 1 — ingest the spec

```bash
meho connector ingest \
  --product vcf-fleet --version 9.0 --impl fleet-rest \
  --spec /path/to/vcf-fleet-9.0/<lcm-api>.yaml \
  --json
```

Or using the `docs:<connector-id>/<file>` shorthand the CLI resolves
against `$CLAUDE_RDC_DOCS`:

```bash
meho connector ingest \
  --product vcf-fleet --version 9.0 --impl fleet-rest \
  --spec docs:vcf-fleet-9.0/<lcm-api>.yaml \
  --json
```

Expected (paraphrased) response shape:

```json
{
  "ingestion": {
    "connector_id": "fleet-rest-9.0",
    "inserted_count": 600,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "fleet-rest-9.0",
    "groups_created": 9,
    "operations_assigned": 540,
    "operations_unassigned": 60,
    "llm_call_count": 14,
    "llm_duration_ms": 22000.0
  }
}
```

Numbers are approximate; the load-bearing checks are
`inserted_count >= 400`, `6 <= groups_created <= 14`, and
`operations_unassigned / inserted_count < 50%`. `connector_registered`
is `false` because `VcfFleetConnector` is already registered in the
v2 registry at module-import time (G3.6-T7) — the auto-shim finds
the existing entry and short-circuits.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review fleet-rest-9.0
```

Expected: a rendered table of 6-14 groups (the path-prefix classifier
maps to 6 named groups; the LLM may propose extras for tails the
classifier doesn't catch — `/lcm/locker/`, `/lcm/authzn/`, etc.).
Compare against the canonical 6 groups in
[`FLEET_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/vcf_fleet/core_ops.py):

| group_key | name | covers |
|---|---|---|
| `fleet-about` | VCF Fleet (about) | `/lcm/lcops/api/v2/about` (broken in 9.0) |
| `fleet-datacenter` | VCF Fleet Datacenters | `/lcm/lcops/api/v2/datacenters` |
| `fleet-vcenter` | VCF Fleet vCenters | `/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters` |
| `fleet-environment` | VCF Fleet Environments | `/lcm/lcops/api/v2/environments` + `…/{environmentId}` |
| `fleet-product` | VCF Fleet Products | `/lcm/lcops/api/v2/environments/{environmentId}/products` |
| `fleet-request` | VCF Fleet Lifecycle Requests | `/lcm/request/api/v2/requests` + `…/{requestId}` |

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_fleet_core_curation`. From a
backplane Python shell:

```python
from meho_backplane.connectors.vcf_fleet import apply_fleet_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_fleet_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `FLEET_CORE_GROUPS`:

1. `ReviewService.edit_group(group_key, name=…, when_to_use=…)` —
   lands the operator-reviewed text the agent reads through
   `list_operation_groups`.
2. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to
   `is_enabled=True` while skipping operator-overridden non-core ops
   (the audit-log-driven exclusion path).

Then for every entry in `FLEET_CORE_OPS`:

3. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands the
   per-op JSON blob the agent inlines into reasoning context when
   the op surfaces in `search_operations` hits.

Each op's `llm_instructions` is a three-key blob (`when_to_call` /
`output_shape` / `next_step`) matching the typed-connector convention
from [`connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
and the ingested-connector convention from
[`connectors/nsx/core_ops.py`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py)
/ [`connectors/harbor/core_ops.py`](../../backend/src/meho_backplane/connectors/harbor/core_ops.py).

### Step 4 — verify the curation

```bash
meho operation groups fleet-rest-9.0
meho operation search fleet-rest-9.0 "list fleet environments" --limit 5
meho operation search fleet-rest-9.0 "what fleet upgrade requests are running" --limit 5
```

The first command should return 6 enabled groups, each with the
canonical `when_to_use` from `FLEET_CORE_GROUPS`. The second should
return `GET:/lcm/lcops/api/v2/environments` in the top-3. The third
should return `GET:/lcm/request/api/v2/requests` in the top-3 (and
ideally `…/{requestId}` close behind).

Every other Fleet op the spec ingestion produced should be in the
`is_enabled=False` state — `search_operations` filters on
`OperationGroup.review_status='enabled'` AND
`EndpointDescriptor.is_enabled=True`, so a staged group's ops never
surface.

### Step 5 — dispatch one list op end-to-end

Against a real probed Fleet target:

```bash
meho operation call fleet-rest-9.0 \
  'GET:/lcm/lcops/api/v2/environments' \
  --target fleet-canary --json
```

Expected: JSON-shaped response carrying an array of Environment
entries (each with `environmentId`, `environmentName`,
`environmentStatus`, and a brief `products[]` summary). The HTTP
Basic auth flow runs implicitly on every request — no session
cache to warm.

The JSONFlux *seam* (would-be handle threading) is exercised
non-interactively at the substrate level; real JSONFlux reduction
(set-shaped payload reduction, MinIO/S3 spill, `result_query`
meta-tool) is **out of scope for v0.5** per Goal #214 — the seam
test proves the dispatcher is ready for it once the production
reducer ships.

### Step 6 — verify fleet.about's fallback guidance

`fleet.about` is listed in the read core for spec parity, but its
underlying endpoint returns HTTP 500 in VCF 9.0. Confirm the curated
`llm_instructions` carries the fallback guidance:

```bash
meho operation describe fleet-rest-9.0 'GET:/lcm/lcops/api/v2/about'
```

The output's `llm_instructions.next_step` must mention
`fleet.datacenter.list` as the fallback reachability probe. If the
appliance is on a patched 9.0 build where `/about` works again, the
operator may re-curate with refreshed prose; the regression note
stays in until Broadcom ships an appliance fix.

## Rollback

If a canary run discovers a regression in the ingestion pipeline or
the curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable fleet-rest-9.0 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every op
   to `is_enabled=False`. The agent meta-tools stop surfacing the
   connector; no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is sufficient
   to reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in T2
   means rows whose parser output didn't change stay untouched,
   while changed rows get an updated revision. After re-curation +
   enable, the agent path re-warms.

## Known gaps

### 1. `/about` regression is a Fleet-appliance issue, not a MEHO bug

The curated `fleet.about` op points at the spec's `/about` endpoint;
the spec is correct, and the connector / agent are correct. The HTTP
500 is a Fleet appliance regression in VCF 9.0 builds. The
`llm_instructions` carries the fallback guidance; when Broadcom
ships an appliance patch that restores `/about`, the curation prose
can be refreshed and the workaround note removed.

### 2. Env-gated automated canary is a follow-up

G3.6-T8 (#835) ships the operator-review substrate
(`apply_fleet_core_curation` helper), the curated 8-op data
(`FLEET_CORE_OPS`), the SQLite-backed curation acceptance tests, and
this runbook. The env-gated automated canary that mirrors
`test_g07_vsphere_canary.py` for Fleet — drives the full ingest
through `IngestionPipelineService` against a real LLM stub or live
Anthropic adapter — is a follow-up.

### 3. Goal #214 G3.6 Fleet checklist line

The Goal body's `[ ] #369 — G3.6 tier-3 VCF management plane`
checkbox flips when the Initiative's whole DoD is met. G3.6-T8 moves
the Initiative forward but does not on its own complete it (G3.6-T9
#839 wires the CLI verbs + the full recorded-fixture E2E). The
checklist tick lives on the Initiative-level wrap-up, not on this
Task.

### 4. CLI verbs and onboarding doc

`apply_fleet_core_curation` is exposed at the Python level (the
substrate the curation helper drives). The `meho vcf-fleet …` CLI
verb tree and the operator-facing
`docs/cross-repo/vcf-fleet-onboarding.md` are deferred to
G3.6-T9 #839.

## References

- Issue: [G3.6-T8 #835](https://github.com/evoila/meho/issues/835).
- Parent Initiative: [G3.6 #369](https://github.com/evoila/meho/issues/369).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.6-T7 #831](https://github.com/evoila/meho/issues/831)
  — `VcfFleetConnector` skeleton.
- Shared scaffolding: [G3.6-T13 #841](https://github.com/evoila/meho/issues/841).
- NSX counterpart: [`g35-nsx-canary.md`](./g35-nsx-canary.md).
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- vRSLCM REST API reference — Broadcom developer portal:
  <https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager/latest/>
- Curated data:
  [`backend/src/meho_backplane/connectors/vcf_fleet/core_ops.py`](../../backend/src/meho_backplane/connectors/vcf_fleet/core_ops.py).
- Codebase doc: [`docs/codebase/connectors-vcf-fleet.md`](../codebase/connectors-vcf-fleet.md).
- Consumer wrapper this contract retires (per Initiative #369):
  `scripts/vcf-fleet.sh` in the consumer's `claude-rdc-hetzner-dc`
  repository (private to the `evoila-bosnia` org).

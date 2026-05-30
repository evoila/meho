<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.5 NSX canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
NSX 4.2 OpenAPI surface through the G0.7 spec-ingestion pipeline
and curating the read-only v0.2 core (~9 ops) the agent surfaces
through `search_operations` / `call_operation`. Run this procedure
when standing up an NSX target against a fresh deploy, when
re-running after a connector / spec revision, or when verifying
the curation against a staging NSX manager.

Companion to
[`docs/cross-repo/g07-vsphere-canary.md`](./g07-vsphere-canary.md)
(the vSphere counterpart this runbook mirrors) and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The NSX-specific delta
is the session-auth flow, the two-spec ingest layout
(`policy.yaml` + `manager.yaml`), and the curated 9-op read core.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven
against the **two-spec NSX corpus**:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests both `nsx-4.2/policy.yaml` (the modern policy API,
   ~1,800 operations across segments / tier-0s / tier-1s /
   firewall) and `nsx-4.2/manager.yaml` (the legacy manager API,
   ~3,200 operations covering node / cluster / transport-node /
   the broader manager surface) under one
   `connector_id="nsx-rest-4.2"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   `endpoint_descriptor` table under the
   `(product, version, impl_id) = ("nsx", "4.2", "nsx-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`NsxConnector`](../../backend/src/meho_backplane/connectors/nsx/connector.py)
   already registered (G3.5-T1 #613); the auto-shim's idempotency
   check (`ensure_connector_class_registered`) short-circuits.
   Every row carries a `spec:<source>` tag so operators can
   distinguish policy-sourced ops from manager-sourced ops via
   `meho connector review`.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable
   `when_to_use` hints + per-op group assignments. The
   path-prefix classifier in
   [`meho_backplane.connectors.nsx.core_ops.NSX_PATH_RULES`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py)
   names the 8 groups the curated core spans.
4. **Curate.** The operator drives `apply_nsx_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector
   to land the 9 read-only core ops + their guidance blobs.
5. **Verify.** The operator dispatches one list op through
   `call_operation` to prove the end-to-end agent path works.
   The JSONFlux *seam* is exercised via the test-only
   `ForceHandleReducer` pattern (see acceptance tests below);
   real JSONFlux reduction is a v0.2.next concern per Goal #214
   scope.

## Prerequisites

- **The NSX OpenAPI specs checked out locally.** The canary's
  spec resolver pattern mirrors
  [`tests/acceptance/_vcenter_spec.py`](../../backend/tests/acceptance/_vcenter_spec.py).
  Set:
  - `MEHO_NSX_OPENAPI_POLICY` + `MEHO_NSX_OPENAPI_MANAGER` —
    absolute paths to `nsx-4.2/policy.yaml` and
    `nsx-4.2/manager.yaml`. Both env vars set → the canary
    drives the two-spec ingest.
  - `MEHO_CONSUMER_DOCS_ROOT` — directory containing
    `nsx-4.2/policy.yaml` and `nsx-4.2/manager.yaml`. The
    consumer's spec-shelf repo is the conventional source.

  The env-gated **automated** canary acceptance test that drives
  the full two-spec ingest against `IngestionPipelineService` is
  a follow-up to G3.5-T2; until it lands, the operator runs this
  procedure manually + the substrate-level dispatch tests live
  at:
  - [`backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py`](../../backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py)
    — 9 parametrised cases proving each curated op dispatches.
  - [`backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py)
    — JSONFlux dispatcher seam proof for the segment-list op.

- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses
  the `pgvector/pgvector:pg16`-derived chart image.
- **A running backplane with `meho connector ingest` available**
  (T5, #486). The connector CLI talks to the REST API at
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
  [`docs/codebase/spec-ingestion.md` §"LLM-client wiring
  (build-time-only today)"](../codebase/spec-ingestion.md#llm-client-wiring-build-time-only-today)
  for the operator-facing framing.
- **A Vault path holding the NSX service-account credentials.**
  The `NsxConnector.session_loader` resolves
  `target.secret_ref` to a `{"username": ..., "password": ...}`
  pair posted as `j_username` / `j_password` against
  `/api/session/create`. Default loader raises
  `NotImplementedError` until G0.3 (#224) lands operator-context
  Vault reads; production deploys pre-G0.3 inject a loader at
  construction.

## NSX auth divergence from vSphere

Unlike vCenter REST (which accepts HTTP Basic), NSX behind the
VCF 9 envoy proxy rejects Basic on the canonical FQDN. The only
auth mode that works across both VCF-9-fronted and standalone
NSX-T managers is the session-cookie + `X-XSRF-TOKEN` flow:

1. `POST /api/session/create` with form-encoded
   `j_username` / `j_password` (httpx's `client.post(url,
   data=<dict>)` produces `application/x-www-form-urlencoded`).
2. The response carries `Set-Cookie: JSESSIONID=…` (cached in
   the per-target httpx client jar) and `X-XSRF-TOKEN: …`
   (cached in the connector's `_session_tokens` dict).
3. Every subsequent request carries both the cookie and the
   header — either one alone is rejected.
4. On HTTP 401, `_get_json_with_session_retry` invalidates the
   token + clears the cookie jar, re-establishes the session
   once, and retries. A second 401 surfaces as a clear
   `RuntimeError` naming the target — the consumer wrapper's
   posture: re-login once, never loop.

This flow is fully exercised by the `NsxConnector` skeleton
G3.5-T1 #613 landed; the dispatch smoke acceptance test
([`test_g35_nsx_dispatch_smoke.py`](../../backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py))
proves it works against a respx-mocked NSX manager.

## Operator procedure

### Step 1 — ingest the specs

```bash
meho connector ingest \
  --product nsx --version 4.2 --impl nsx-rest \
  --spec /path/to/nsx-4.2/policy.yaml \
  --spec /path/to/nsx-4.2/manager.yaml \
  --json
```

Or using the `docs:<connector-id>/<file>` shorthand the CLI
resolves against `$CLAUDE_RDC_DOCS`:

```bash
meho connector ingest \
  --product nsx --version 4.2 --impl nsx-rest \
  --spec docs:nsx-4.2/policy.yaml \
  --spec docs:nsx-4.2/manager.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "nsx-rest-4.2",
    "inserted_count": 5000,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "nsx-rest-4.2",
    "groups_created": 14,
    "operations_assigned": 4200,
    "operations_unassigned": 800,
    "llm_call_count": 102,
    "llm_duration_ms": 165000.0
  }
}
```

Numbers are approximate; the load-bearing checks are
`inserted_count >= 4500`, `8 <= groups_created <= 18`, and
`operations_unassigned / inserted_count < 50%`. `connector_registered`
is `false` here because `NsxConnector` is already registered in
the v2 registry at module-import time (G3.5-T1) — the auto-shim
finds the existing entry and short-circuits.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review nsx-rest-4.2
```

Expected: a rendered table of 8-18 groups (the path-prefix
classifier maps to 8 named groups; the LLM may propose extras
for tails the classifier doesn't catch). Compare against the
canonical 8 groups in
[`NSX_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py):

| group_key | name | covers |
|---|---|---|
| `manager-node` | NSX Manager (node) | `/api/v1/node` |
| `manager-cluster` | NSX Manager (cluster) | `/api/v1/cluster/status` |
| `manager-transport-nodes` | NSX Transport Nodes | `/api/v1/transport-nodes` |
| `policy-segments` | NSX Segments | `/policy/api/v1/infra/segments` |
| `policy-transport-zones` | NSX Transport Zones | `/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones` |
| `policy-tier0` | NSX Tier-0 Gateways | `/policy/api/v1/infra/tier-0s` |
| `policy-tier1` | NSX Tier-1 Gateways | `/policy/api/v1/infra/tier-1s` |
| `policy-firewall` | NSX Distributed Firewall | `/policy/api/v1/infra/domains/{domain-id}/security-policies` + `…/rules` |

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_nsx_core_curation`. From a
backplane Python shell:

```python
from meho_backplane.connectors.nsx import apply_nsx_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_nsx_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `NSX_CORE_GROUPS`:

1. `ReviewService.edit_group(group_key, name=…, when_to_use=…)`
   — lands the operator-reviewed text the agent reads through
   `list_operation_groups`.
2. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to
   `is_enabled=True`.

Then for every entry in `NSX_CORE_OPS`:

3. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands
   the per-op JSON blob the agent inlines into reasoning context
   when the op surfaces in `search_operations` hits.

Each op's `llm_instructions` is a three-key blob
(`when_to_call` / `output_shape` / `next_step`) matching the
typed-connector convention from
[`connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
and [`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py).
The same agent reads both surfaces, so the structure stays
uniform across typed and ingested connectors.

### Step 4 — verify the curation

```bash
meho operation groups nsx-rest-4.2
meho operation search nsx-rest-4.2 "list nsx segments" --limit 5
meho operation search nsx-rest-4.2 "what nsx firewall rules exist in domain X" --limit 5
```

The first command should return 8 enabled groups, each with the
canonical `when_to_use` from `NSX_CORE_GROUPS`. The second
should return `GET:/policy/api/v1/infra/segments` in the top-3.
The third should return
`GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules`
in the top-3.

Every other NSX op the spec ingestion produced should be in the
`is_enabled=False` state — `search_operations` filters on
`OperationGroup.review_status='enabled'` AND
`EndpointDescriptor.is_enabled=True`, so a staged group's ops
never surface.

### Step 5 — dispatch one list op end-to-end

Against a real probed NSX target:

```bash
meho operation call nsx-rest-4.2 \
  'GET:/policy/api/v1/infra/segments' \
  --target nsx-canary --json
```

Expected: JSON-shaped response carrying a `results` array of
segment entries. The session-create + XSRF flow runs implicitly
on the first dispatch and the per-target cache reuses the token
for subsequent ones.

The JSONFlux *seam* (would-be handle threading) is exercised
non-interactively at
[`backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py):
the test installs a `ForceHandleReducer` that always wraps the
NSX list payload in a synthetic `ResultHandle` and asserts the
dispatcher's `OperationResult.handle` carries it through. Real
JSONFlux reduction (set-shaped payload reduction, MinIO/S3
spill, `result_query` meta-tool) is **out of scope for v0.2**
per Goal #214 — the seam test proves the dispatcher is ready
for it once the production reducer ships.

## Rollback

If a canary run discovers a regression in the ingestion pipeline
or the curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable nsx-rest-4.2 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every
   op to `is_enabled=False`. The agent meta-tools stop surfacing
   the connector; no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is
   sufficient to reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in
   T2 means rows whose parser output didn't change stay
   untouched, while changed rows get an updated revision. After
   re-curation + enable, the agent path re-warms.

## Known gaps

### 1. Env-gated automated two-spec canary is a follow-up

G3.5-T2 (#614) ships the operator-review substrate
(`apply_nsx_core_curation` helper, `edit_op(llm_instructions=…)`
extension), the curated 9-op data (`NSX_CORE_OPS`), the dispatch
smoke + JSONFlux force-handle acceptance tests over respx-mocked
NSX, and this runbook. The env-gated automated canary that
mirrors `test_g07_vsphere_canary.py` for NSX — drives the full
two-spec ingest of `policy.yaml` + `manager.yaml` through
`IngestionPipelineService` against a real LLM stub or live
Anthropic adapter — is a follow-up. It requires the NSX spec
files reachable from CI, the same env-gated pattern
`tests/acceptance/_vcenter_spec.py` codifies for vSphere.

### 2. Goal #214 G3.5 NSX checklist line

The Goal body's `[ ] #368 — G3.5 tier-2 batch — all generic`
checkbox flips when the Initiative's whole DoD is met. G3.5-T2
moves the Initiative forward but does not on its own complete
it (G3.5-T3 #615 wires the CLI + MCP review verbs + the full
recorded-fixture E2E). The checklist tick lives on the
Initiative-level wrap-up, not on this Task.

### 3. CLI verbs for per-op `llm_instructions` editing

`ReviewService.edit_op(llm_instructions=…)` is exposed at the
Python level (the substrate the curation helper drives). The
CLI verb `meho connector edit-op --llm-instructions <json>` and
the matching REST / MCP route extensions are deferred to G3.5-T3
#615 alongside the CLI verbs for NSX-specific operations.

## References

- Issue: [G3.5-T2 #614](https://github.com/evoila/meho/issues/614).
- Parent Initiative: [G3.5 #368](https://github.com/evoila/meho/issues/368).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.5-T1 #613](https://github.com/evoila/meho/issues/613)
  — `NsxConnector` skeleton.
- vSphere counterpart: [`g07-vsphere-canary.md`](./g07-vsphere-canary.md).
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- NSX REST API reference — Broadcom developer portal:
  https://developer.broadcom.com/xapis/nsx-data-center-rest-api/latest/
- Curated data:
  [`backend/src/meho_backplane/connectors/nsx/core_ops.py`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py).
- Codebase doc: [`docs/codebase/connectors-nsx.md`](../codebase/connectors-nsx.md).
- Consumer wrapper this contract retires (per Initiative #368):
  `scripts/nsx.sh` in the consumer's `claude-rdc-hetzner-dc`
  repository (private to the `evoila-bosnia` org).

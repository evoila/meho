<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.6 vRLI canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
vRLI 9.x OpenAPI surface through the G0.7 spec-ingestion pipeline
and curating the read-only v0.5 core (7 ops) the agent surfaces
through `search_operations` / `call_operation`. Run this procedure
when standing up a vRLI target against a fresh deploy, when
re-running after a connector / spec revision, or when verifying the
curation against a staging vRLI cluster.

Companion to
[`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md)
(the NSX counterpart this runbook mirrors) and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The vRLI-specific delta is
the session-token Bearer auth flow (verified against the consumer's
`scripts/vcf-logs.sh` wrapper), the single-spec ingest layout
(`vcf-logs-9.0/api-v2.yaml`), and the curated 7-op read core biased
toward the headline log-query surfaces.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven
against the **vRLI v2 API corpus**:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests `vcf-logs-9.0/api-v2.yaml` (the vRLI 9.x REST API spec
   spanning event query, aggregated query, fields catalog, hosts,
   content packs, alerts, sessions, ingest, and configuration
   surfaces) under one `connector_id="vrli-rest-9.0"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the
   `endpoint_descriptor` table under the
   `(product, version, impl_id) = ("vrli", "9.0", "vrli-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`VcfLogsConnector`](../../backend/src/meho_backplane/connectors/vcf_logs/connector.py)
   already registered (G3.6-T4 #830); the auto-shim's idempotency
   check (`ensure_connector_class_registered`) short-circuits.
   Every row carries a `spec:vcf-logs-9.0/api-v2.yaml` tag so
   operators can audit the spec source via `meho connector review`.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable
   `when_to_use` hints + per-op group assignments. The path-prefix
   classifier in
   [`meho_backplane.connectors.vcf_logs.core_ops.VRLI_PATH_RULES`](../../backend/src/meho_backplane/connectors/vcf_logs/core_ops.py)
   names the 5 groups the curated core spans (`vrli-system`,
   `vrli-events`, `vrli-inventory`, `vrli-content`,
   `vrli-alerts`).
4. **Curate.** The operator drives `apply_vrli_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector to
   land the 7 read-only core ops + their guidance blobs.
5. **Verify.** The operator dispatches one list op through
   `call_operation` to prove the end-to-end agent path works.
   The JSONFlux *seam* (used by the event-query op when result
   sets are large) is exercised non-interactively through the
   per-op `output_shape` + `next_step` text — every `vrli.event.query`
   reference in the curated `llm_instructions` directs the agent
   to `result_describe` / `result_query` for the actual rows.
   Real JSONFlux reduction is a v0.5.next concern per Goal #214
   scope.

## Prerequisites

- **The vRLI OpenAPI spec checked out locally.** The canary's
  spec resolver pattern mirrors
  [`tests/acceptance/_vcenter_spec.py`](../../backend/tests/acceptance/_vcenter_spec.py).
  Set:
  - `MEHO_VRLI_OPENAPI` — absolute path to
    `vcf-logs-9.0/api-v2.yaml`, or
  - `MEHO_CONSUMER_DOCS_ROOT` — directory containing
    `vcf-logs-9.0/api-v2.yaml`. The consumer's spec-shelf repo is
    the conventional source.

  The env-gated **automated** canary acceptance test that drives
  the ingest against `IngestionPipelineService` is a follow-up to
  G3.6-T5; until it lands, the operator runs this procedure
  manually + the substrate-level dispatch tests live at:
  - [`backend/tests/test_connectors_vcf_logs_core_ops.py`](../../backend/tests/test_connectors_vcf_logs_core_ops.py)
    — classifier + curation behavioural suite covering the 7
    curated ops, the read-only invariant (non-`GET` methods
    classify as `none`), and the JSONFlux-handle advertisement
    on `vrli.event.query`.
- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses
  the `pgvector/pgvector:pg16`-derived chart image.
- **A running backplane with `meho connector ingest` available**
  (T5, #486). The connector CLI talks to the REST API at
  `http(s)://<backplane>/api/v1/connectors/ingest`.
- **An LLM client configured for the grouping pass.** Production
  deploys wire the Anthropic Messages-API adapter under
  `IngestionPipelineService(..., llm_client_factory=...)`.
- **A Vault path holding the vRLI service-account credentials.**
  The `VcfLogsConnector` resolves `target.secret_ref` to a
  `{"username": ..., "password": ...}` pair posted to
  `POST /api/v2/sessions` with the per-target `provider` field
  (`Local` default; `ActiveDirectory` / `vIDM` are documented
  alternatives — only `Local` + `ActiveDirectory` are supported
  in v0.5). Default loader raises `NotImplementedError` until
  G0.3 (#224) lands operator-context Vault reads; production
  deploys pre-G0.3 inject a loader at construction.

## vRLI auth divergence from NSX

Unlike NSX (cookie + XSRF-token paired) and unlike vROps / Fleet
(stateless HTTP Basic on every request), vRLI uses a **session-token
Bearer** flow:

1. `POST /api/v2/sessions` with JSON body
   `{"username": ..., "password": ..., "provider": "Local"}` and
   `Content-Type: application/json`. Returns
   `{"sessionId": "<token>", "ttl": <seconds>}`.
2. Every subsequent request carries
   `Authorization: Bearer <sessionId>` — no cookie, no XSRF token.
3. On HTTP 401, `_get_json_with_session_retry` invalidates the
   cached token, re-establishes the session once, and retries.
   A second 401 surfaces as a clear `RuntimeError` naming the
   target — same posture the NSX precedent established (re-login
   once, never loop). Per-target token isolation via the
   `_session_tokens: dict[str, str]` cache in
   [`VcfLogsConnector`](../../backend/src/meho_backplane/connectors/vcf_logs/connector.py).

This flow is fully exercised by the `VcfLogsConnector` skeleton
G3.6-T4 #830 landed; the dispatch path is exercised against a
respx-mocked vRLI through the standard
[`HttpConnector`](../../backend/src/meho_backplane/connectors/adapters/http.py)
+ G0.6 dispatcher.

## Operator procedure

### Step 1 — ingest the spec

```bash
meho connector ingest \
  --product vrli --version 9.0 --impl vrli-rest \
  --spec docs:vcf-logs-9.0/api-v2.yaml \
  --json
```

Or with an absolute path:

```bash
meho connector ingest \
  --product vrli --version 9.0 --impl vrli-rest \
  --spec /path/to/vcf-logs-9.0/api-v2.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "vrli-rest-9.0",
    "inserted_count": 240,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "vrli-rest-9.0",
    "groups_created": 8,
    "operations_assigned": 220,
    "operations_unassigned": 20,
    "llm_call_count": 6,
    "llm_duration_ms": 15000.0
  }
}
```

Numbers are approximate; the load-bearing checks are
`inserted_count >= 100`, `5 <= groups_created <= 18`, and
`operations_unassigned / inserted_count < 50%`. `connector_registered`
is `false` here because `VcfLogsConnector` is already registered in
the v2 registry at module-import time (G3.6-T4) — the auto-shim
finds the existing entry and short-circuits.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review vrli-rest-9.0
```

Expected: a rendered table of 5-18 groups (the path-prefix
classifier maps to 5 named groups; the LLM may propose extras for
tails the classifier doesn't catch, e.g. `/api/v2/sessions`,
`/api/v2/notification`, `/api/v2/config`). Compare against the
canonical 5 groups in
[`VRLI_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/vcf_logs/core_ops.py):

| group_key | name | covers |
|---|---|---|
| `vrli-system` | vRLI (system + indexer catalog) | `/api/v2/version` + `/api/v2/fields` |
| `vrli-events` | vRLI Event Queries | `/api/v2/events/{constraints}` + `/api/v2/aggregated-events/{constraints}` |
| `vrli-inventory` | vRLI Hosts | `/api/v2/hosts` |
| `vrli-content` | vRLI Content Packs | `/api/v2/content/contentpack/list` |
| `vrli-alerts` | vRLI Alert Definitions | `/api/v2/alerts` |

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_vrli_core_curation`. From a
backplane Python shell:

```python
from meho_backplane.connectors.vcf_logs import apply_vrli_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_vrli_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `VRLI_CORE_GROUPS`:

1. `ReviewService.edit_group(group_key, name=…, when_to_use=…)`
   — lands the operator-reviewed text the agent reads through
   `list_operation_groups`.
2. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to
   `is_enabled=True` (except for non-core ops the helper
   explicitly disabled in step 0 via the audit-log-driven
   operator-override exclusion).

Then for every entry in `VRLI_CORE_OPS`:

3. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands
   the per-op JSON blob the agent inlines into reasoning context
   when the op surfaces in `search_operations` hits.

Each op's `llm_instructions` is a three-key blob
(`when_to_call` / `output_shape` / `next_step`) matching the
typed-connector convention from
[`connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
and [`connectors/harbor/core_ops.py`](../../backend/src/meho_backplane/connectors/harbor/core_ops.py).
The same agent reads both surfaces, so the structure stays
uniform across typed and ingested connectors.

### Step 4 — verify the curation

```bash
meho operation groups vrli-rest-9.0
meho operation search vrli-rest-9.0 "query vrli for nsx error events" --limit 5
meho operation search vrli-rest-9.0 "how many alerts are configured" --limit 5
meho operation search vrli-rest-9.0 "what hosts are reporting logs" --limit 5
```

The first command should return 5 enabled groups, each with the
canonical `when_to_use` from `VRLI_CORE_GROUPS`. The second should
return `GET:/api/v2/events/{constraints}` in the top-3. The third
should return `GET:/api/v2/alerts` in the top-3. The fourth should
return `GET:/api/v2/hosts` in the top-3.

Every other vRLI op the spec ingestion produced should be in the
`is_enabled=False` state — `search_operations` filters on
`OperationGroup.review_status='enabled'` AND
`EndpointDescriptor.is_enabled=True`, so a staged group's ops
never surface.

### Step 5 — dispatch one read op end-to-end

Against a real probed vRLI target:

```bash
meho operation call vrli-rest-9.0 \
  'GET:/api/v2/version' \
  --target vrli-canary --json
```

Expected: JSON-shaped response carrying `version`, `releaseName`,
and `build`. The session-create flow runs implicitly on the first
dispatch and the per-target cache reuses the token for subsequent
ones.

For the JSONFlux-handle path (large event-query results), dispatch
the event-query op with a constraint that the indexer would
typically return many rows for:

```bash
meho operation call vrli-rest-9.0 \
  'GET:/api/v2/events/{constraints}' \
  --target vrli-canary \
  --param constraints='timestamp/last-1h' \
  --json
```

When the production reducer is wired (v0.5.next), large event
result sets will return as a `ResultHandle`; the agent reads
them via `result_describe` + `result_query`. v0.5 ships only the
`PassThroughReducer`, so handles are exercised non-interactively
via the per-op `llm_instructions` advertising the shape (assert
covered by
[`test_vrli_core_groups_event_query_op_is_jsonflux_handle_shaped`](../../backend/tests/test_connectors_vcf_logs_core_ops.py)).

## Rollback

If a canary run discovers a regression in the ingestion pipeline
or the curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable vrli-rest-9.0 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every
   op to `is_enabled=False`. The agent meta-tools stop surfacing
   the connector; no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is sufficient
   to reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in
   T2 means rows whose parser output didn't change stay
   untouched, while changed rows get an updated revision. After
   re-curation + enable, the agent path re-warms.

## Known gaps

### 1. Env-gated automated canary is a follow-up

G3.6-T5 (#834) ships the operator-review substrate
(`apply_vrli_core_curation` helper), the curated 7-op data
(`VRLI_CORE_OPS`), the classifier + curation behavioural tests
over a SQLite-backed seeded substrate, and this runbook. The
env-gated automated canary that mirrors
`test_g07_vsphere_canary.py` for vRLI — drives the full ingest
of `vcf-logs-9.0/api-v2.yaml` through `IngestionPipelineService`
against a real LLM stub or live Anthropic adapter — is a
follow-up. It requires the vRLI spec file reachable from CI, the
same env-gated pattern `tests/acceptance/_vcenter_spec.py`
codifies for vSphere.

### 2. Goal #214 G3.6 vRLI checklist line

The Goal body's `[ ] #369 — G3.6 tier-3 VCF management plane`
checkbox flips when the Initiative's whole DoD is met. G3.6-T5
moves the Initiative forward but does not on its own complete
it (G3.6-T6 #838 wires the CLI verbs + MCP review verbs + the
full recorded-fixture E2E for vRLI). The checklist tick lives
on the Initiative-level wrap-up, not on this Task.

### 3. CLI verbs for per-op `llm_instructions` editing

`ReviewService.edit_op(llm_instructions=…)` is exposed at the
Python level (the substrate the curation helper drives). The
CLI verb `meho connector edit-op --llm-instructions <json>` and
the matching REST / MCP route extensions are deferred to
G3.6-T6 #838 alongside the CLI verbs for vRLI-specific
operations (`meho vcf-logs query`, etc.).

### 4. Write ops stay staged

The curated v0.5 core is deliberately read-only. vRLI write ops
(alert create / update / delete, content-pack import, query
result export) remain in their respective groups but stay
`is_enabled=False` — the classifier's `GET`-only gate (see
[`classify_vrli_op`](../../backend/src/meho_backplane/connectors/vcf_logs/core_ops.py))
ensures no non-GET op lands under a curated group. Lifting any
write op to `enabled` requires a follow-up Task with explicit
operator review.

## References

- Issue: [G3.6-T5 #834](https://github.com/evoila/meho/issues/834).
- Parent Initiative: [G3.6 #369](https://github.com/evoila/meho/issues/369).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.6-T4 #830](https://github.com/evoila/meho/issues/830)
  — `VcfLogsConnector` skeleton.
- NSX counterpart: [`g35-nsx-canary.md`](./g35-nsx-canary.md).
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- vRLI REST API reference — Broadcom developer portal:
  https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/
- Curated data:
  [`backend/src/meho_backplane/connectors/vcf_logs/core_ops.py`](../../backend/src/meho_backplane/connectors/vcf_logs/core_ops.py).
- Codebase doc: [`docs/codebase/connectors-vcf-logs.md`](../codebase/connectors-vcf-logs.md).
- Consumer wrapper this contract retires (per Initiative #369):
  `scripts/vcf-logs.sh` in the consumer's `claude-rdc-hetzner-dc`
  repository (private to the `evoila-bosnia` org).

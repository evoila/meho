<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G3.7 Hetzner Robot canary — operator procedure

This document is the **operator-facing runbook** for ingesting the
Hetzner Robot Webservice OpenAPI spec through the G0.7 spec-ingestion
pipeline and curating the read-only v0.2 core (~10 ops) the agent
surfaces through `search_operations` / `call_operation`. Run this
procedure when standing up a Hetzner Robot target against a fresh
deploy, when re-running after a connector or spec revision, or when
verifying the curation against a staging Robot Webservice account.

Companion to
[`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md)
(the NSX counterpart this runbook mirrors) and
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
(the connector-agnostic ingest runbook). The Hetzner-Robot-specific
delta is the HTTP Basic auth flow, the single-spec ingest layout
(`robot-api.yaml`), and the curated 10-op read core.

## What this canary proves

End-to-end correctness of the G0.7 ingestion pipeline driven against the
**single-spec Hetzner Robot corpus**:

1. **Parse.** The T1 parser
   ([`meho_backplane.operations.ingest.parse_openapi`](../../backend/src/meho_backplane/operations/ingest/openapi.py))
   ingests `hetzner-robot-2026-04/<robot-api>.yaml` (the Robot Webservice
   OpenAPI spec, containing the full set of dedicated-server management
   endpoints) under one `connector_id="hetzner-rest-2026.04"`.
2. **Register.** T2's
   [`register_ingested_operations`](../../backend/src/meho_backplane/operations/ingest/register_ingested.py)
   bulk-upserts every parsed operation into the `endpoint_descriptor`
   table under the
   `(product="hetzner-robot", version="2026.04", impl_id="hetzner-rest")`
   connector triple. The first ingest finds the hand-rolled
   [`HetznerRobotConnector`](../../backend/src/meho_backplane/connectors/hetzner_robot/connector.py)
   already registered (G3.7-T6 #846); the auto-shim's idempotency check
   (`ensure_connector_class_registered`) short-circuits. Every row carries
   a `spec:hetzner-robot-2026-04/robot-api.yaml` tag.
3. **Group.** T3's
   [`run_llm_grouping`](../../backend/src/meho_backplane/operations/ingest/llm_groups.py)
   pass derives operation groups with operator-readable `when_to_use`
   hints and per-op group assignments. The path-prefix classifier in
   [`meho_backplane.connectors.hetzner_robot.core_ops.ROBOT_PATH_RULES`](../../backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py)
   names the 4 groups the curated core spans.
4. **Curate.** The operator drives `apply_robot_core_curation`
   (which uses `ReviewService.edit_group` + `enable_group` +
   `edit_op(llm_instructions=…)`) against the staged connector to land
   the 10 read-only core ops and their guidance blobs.
5. **Verify.** The operator dispatches one list op through `call_operation`
   to prove the end-to-end agent path works. The JSONFlux seam is
   exercised via the test-only `ForceHandleReducer` pattern (see
   acceptance tests below); real JSONFlux reduction is a v0.2.next
   concern per Goal #214 scope.

## Prerequisites

- **The Hetzner Robot OpenAPI spec checked out locally.** The spec lives
  in the consumer's `docs/hetzner-robot-2026-04/` shelf. Set:
  - `MEHO_ROBOT_OPENAPI` — absolute path to
    `hetzner-robot-2026-04/<robot-api>.yaml`.
  - `MEHO_CONSUMER_DOCS_ROOT` — directory containing
    `hetzner-robot-2026-04/<robot-api>.yaml`. The consumer's spec-shelf
    repo is the conventional source.

  The env-gated automated canary acceptance test that drives the full
  ingest against `IngestionPipelineService` is a follow-up to G3.7-T8;
  until it lands, the operator runs this procedure manually. The
  substrate-level unit tests live at:
  - [`backend/tests/test_connectors_hetzner_robot_core_ops.py`](../../backend/tests/test_connectors_hetzner_robot_core_ops.py)
    — curation substrate tests (10 ops enabled, non-core ops disabled,
    llm_instructions + when_to_use populated, audit rows written).

- **A Postgres instance with pgvector + FTS extensions.** Local
  development uses the testcontainers fixture; production uses the
  `pgvector/pgvector:pg16`-derived chart image.
- **A running backplane with `meho connector ingest` available.**
  The connector CLI talks to the REST API at
  `http(s)://<backplane>/api/v1/connectors/ingest`.
- **An LLM client configured for the grouping pass.** Production deploys
  wire the Anthropic Messages-API adapter under
  `IngestionPipelineService(..., llm_client_factory=...)`.
- **A Vault path holding the Robot Webservice-user credentials.** The
  `HetznerRobotConnector.auth_headers` resolves `target.secret_ref` to a
  `{"username": ..., "password": ...}` pair. **Important:** The Webservice
  user is a distinct account from the Robot login user — it must be
  created in the Robot portal under _Settings → Webservice and app setup_.

## Auth divergence from NSX / Harbor

Hetzner Robot uses **HTTP Basic auth** on every request — there is no
session-cookie or XSRF-token dance (unlike NSX). The Webservice-user
credentials are loaded once from Vault per target and cached. The
Authorization header is recomputed from the cache on each request.

**IP-block protection (critical for shared egress):** Hetzner Robot blocks
the source IP for 10 minutes after 3 consecutive 401 responses. Because
MEHO operates on a shared egress IP, a single misconfigured target could
lock every operator off the Robot API for 10 minutes. The connector raises
`RuntimeError("auth_failed: …")` on the **first** 401 — it never retries,
never consumes the 2 remaining attempts. Fix the Vault secret and restart
the target before retrying.

This flow is fully exercised by the `HetznerRobotConnector` skeleton
G3.7-T6 #846 landed. The connector's `probe()` and `fingerprint()` both
call `GET /server` (the cheapest authenticated endpoint) and propagate
the auth_failed posture.

## Operator procedure

### Step 1 — ingest the spec

```bash
meho connector ingest \
  --product hetzner-robot --version 2026.04 --impl hetzner-rest \
  --spec docs:hetzner-robot-2026-04/<robot-api>.yaml \
  --json
```

Or using the absolute path form:

```bash
meho connector ingest \
  --product hetzner-robot --version 2026.04 --impl hetzner-rest \
  --spec /path/to/hetzner-robot-2026-04/<robot-api>.yaml \
  --json
```

Expected (paraphrased) response:

```json
{
  "ingestion": {
    "connector_id": "hetzner-rest-2026.04",
    "inserted_count": 50,
    "updated_count": 0,
    "skipped_count": 0,
    "connector_registered": false,
    "operations_grouped": false
  },
  "grouping": {
    "connector_id": "hetzner-rest-2026.04",
    "groups_created": 6,
    "operations_assigned": 45,
    "operations_unassigned": 5,
    "llm_call_count": 3,
    "llm_duration_ms": 8000.0
  }
}
```

Numbers are approximate (the Robot API surface is smaller than NSX or
vSphere). The load-bearing checks are `inserted_count >= 30`,
`4 <= groups_created <= 10`, and
`operations_unassigned / inserted_count < 30%`. `connector_registered`
is `false` because `HetznerRobotConnector` is already registered at
module-import time (G3.7-T6 #846) — the auto-shim finds the existing
entry and short-circuits.

### Step 2 — review the LLM-summarised groups

```bash
meho connector review hetzner-rest-2026.04
```

Expected: a rendered table of 4–10 groups. Compare against the canonical
4 groups in
[`ROBOT_CORE_GROUPS`](../../backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py):

| group_key | name | covers |
|---|---|---|
| `robot-about` | Hetzner Robot (about) | `GET /query` |
| `robot-servers` | Hetzner Robot Dedicated Servers | `GET /server`, `GET /server/{server-ip}` |
| `robot-networking` | Hetzner Robot Networking | `GET /ip`, `/subnet`, `/vswitch`, `/vswitch/{id}`, `/failover`, `/rdns` |
| `robot-ssh-keys` | Hetzner Robot SSH Keys | `GET /key` |

The LLM may propose additional groups for write paths (`/boot`,
`/reset`, `/wol`, `/order`). These are expected and stay `staged` —
the curated read core only enables the 4 groups above.

### Step 3 — apply the curated read-core

The Python entrypoint is `apply_robot_core_curation`. From a backplane
Python shell:

```python
from meho_backplane.connectors.hetzner_robot import apply_robot_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_robot_core_curation(review_service, tenant_id=None)
```

This drives, for every entry in `ROBOT_CORE_GROUPS`:

1. `ReviewService.edit_group(group_key, name=…, when_to_use=…)` — lands
   the operator-reviewed text the agent reads through
   `list_operation_groups`.
2. `ReviewService.enable_group(group_key)` — flips
   `review_status='enabled'`; cascades child ops to `is_enabled=True`.

Then for every entry in `ROBOT_CORE_OPS`:

3. `ReviewService.edit_op(op_id, llm_instructions=…)` — lands the per-op
   JSON blob the agent inlines into reasoning context when the op surfaces
   in `search_operations` hits.

Each op's `llm_instructions` is the canonical three-key blob
(`when_to_call` / `output_shape` / `next_step`) matching the typed-
connector convention.

### Step 4 — verify the curation

```bash
meho operation groups hetzner-rest-2026.04
meho operation search hetzner-rest-2026.04 "list dedicated servers" --limit 5
meho operation search hetzner-rest-2026.04 "which SSH keys are registered" --limit 5
```

The first command should return 4 enabled groups, each with the canonical
`when_to_use` from `ROBOT_CORE_GROUPS`. The second should return
`GET:/server` in the top-3. The third should return `GET:/key` in the top-3.

Every other Robot op the spec ingestion produced should be in the
`is_enabled=False` state.

### Step 5 — dispatch one list op end-to-end

Against a real probed Robot target:

```bash
meho operation call hetzner-rest-2026.04 \
  'GET:/server' \
  --target robot-canary --json
```

Expected: JSON-shaped response carrying the account's dedicated-server
list (or an empty array for a newly-created Webservice user with no
servers yet). HTTP Basic auth runs implicitly on the first dispatch and
the per-target credential cache reuses the pair for subsequent calls.

The JSONFlux seam (would-be handle threading for the `GET:/server` list
response) is exercised non-interactively via the acceptance test's
`ForceHandleReducer` pattern. Real JSONFlux reduction (set-shaped payload
reduction, MinIO/S3 spill, `result_query` meta-tool) is **out of scope
for v0.2** per Goal #214 — the seam test proves the dispatcher is ready
for it once the production reducer ships.

## Rollback

If a canary run discovers a regression in the ingestion pipeline or the
curated content:

1. **Disable the connector immediately:**

   ```bash
   meho connector disable hetzner-rest-2026.04 --confirm
   ```

   Cascades every group to `review_status='disabled'` and every op to
   `is_enabled=False`. The agent meta-tools stop surfacing the connector;
   no in-flight dispatches will use it.

2. **Capture the audit trail.** Every state transition wrote a
   `meho.connector.*` row to `audit_log`; the trail is sufficient to
   reconstruct what happened.

3. **Re-ingest after fix.** Once the pipeline is patched, drive
   `meho connector ingest` again — the body-hash idempotence in T2 means
   rows whose parser output didn't change stay untouched, while changed
   rows get an updated revision. After re-curation + enable, the agent
   path re-warms.

## Known gaps

### 1. Env-gated automated canary is a follow-up

G3.7-T8 (#849) ships the operator-review substrate
(`apply_robot_core_curation` helper, `edit_op(llm_instructions=…)`
extension), the curated 10-op data (`ROBOT_CORE_OPS`), the substrate unit
tests, and this runbook. The env-gated automated canary that drives the
full ingest of `robot-api.yaml` through `IngestionPipelineService` against
a real LLM stub or live Anthropic adapter is a follow-up requiring the
Robot spec file reachable from CI.

### 2. CLI verbs for per-op `llm_instructions` editing

`ReviewService.edit_op(llm_instructions=…)` is exposed at the Python
level. The CLI verb `meho connector edit-op --llm-instructions <json>` and
the matching REST / MCP route extensions are deferred to G3.7-T9 alongside
the CLI verbs for Robot-specific operations.

### 3. Write ops out of scope for v0.2

Boot, reset, WoL, order, and all other write/mutating Robot paths remain
`staged` and are never enabled in v0.2 per the Initiative #370 DoD and the
issue body Out-of-scope section.

## References

- Issue: [G3.7-T8 #849](https://github.com/evoila/meho/issues/849).
- Parent Initiative: [G3.7 #370](https://github.com/evoila/meho/issues/370).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Predecessor: [G3.7-T6 #846](https://github.com/evoila/meho/issues/846)
  — `HetznerRobotConnector` skeleton.
- NSX counterpart: [`g35-nsx-canary.md`](./g35-nsx-canary.md).
- Harbor counterpart: see Initiative #368 docs.
- Connector-agnostic ingest runbook: [`connector-ingestion.md`](./connector-ingestion.md).
- Substrate: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  (operation registry + dispatcher);
  [#389 G0.7](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- Hetzner Robot Webservice API reference:
  https://robot.hetzner.com/doc/webservice/en.html
- Curated data:
  [`backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py`](../../backend/src/meho_backplane/connectors/hetzner_robot/core_ops.py).
- Codebase doc: [`docs/codebase/connectors-hetzner-robot.md`](../codebase/connectors-hetzner-robot.md).

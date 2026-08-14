# Connector: harbor (Harbor 2.x)

## Overview

The `harbor` connector is the hand-rolled `HttpConnector` subclass that
dispatches Harbor REST operations under the
`(product="harbor", version="2.x", impl_id="harbor-rest")` registry triple.
G3.5-T7 (#619) shipped the skeleton — HTTP Basic auth, fingerprint, probe, and
the G0.6 dispatch shim. G3.5-T9 (#621) added the robot lifecycle typed ops
(`harbor.robot.create` / `harbor.robot.delete`) and the `credential_mint` G6
broadcast classifier. G3.5-T10 (#622) added the `meho harbor …` CLI verb tree
(`cli/internal/cmd/harbor/`), the real-container E2E test against
`goharbor/harbor-core:v2.11.0`, and `docs/cross-repo/harbor-onboarding.md`.
Wave-2 (#2857) adds `harbor.artifact.vulnerabilities` — a standalone typed
read for the per-artifact CVE list behind `harbor.artifact.info`'s
`scan_overview` severity counts. Wave-2 (#2858) adds two more standalone
typed storage-quota reads — `harbor.project.summary` (per-project quota/usage
occupancy) and `harbor.quota.list` (fleet-wide project quotas) — and fixes the
`harbor.project.info` overpromise: the vendor Harbor 2.11 `Project` object
carries no `quota` or `chart_count` field, so those claims were dropped
from its `llm_instructions` and repointed at `harbor.project.summary`.

**#2856** converted the 9-op read core from the original ingested-curation
apparatus (a retired `core_ops.py` whose ops only dispatched once a per-deploy
`meho connector ingest` populated the catalog) to **typed** ops
(`source_kind="typed"`) that dispatch on a fresh boot with zero catalog ingest.
This mirrors the NSX (#2302) and SDDC Manager (#2306) conversions under Task
#2358 and closes Goal #2247's per-deploy catalog-state failure class for
Harbor: on a live deploy the whole read surface — and the CLI verbs over it —
now works without any ingest step.

Source: `backend/src/meho_backplane/connectors/harbor/`.

## Key types

- **`HarborConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="harbor"`, `version="2.x"`,
  `impl_id="harbor-rest"`, `supported_version_range=">=2.0,<3.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim (priority=0) defensively if both somehow register for the same
  triple. The connector hosts the bound-method handlers the dispatcher
  resolves and binds to the per-process instance at dispatch time: the 9
  read shims (`about` / `health` / `project_list` / `project_info` /
  `repository_list` / `repository_info` / `artifact_list` / `artifact_info`
  / `robot_list`, each delegating to a `typed_reads` body), the two robot
  writes (`robot_create` / `robot_delete`), the standalone
  `artifact_vulnerabilities` CVE-detail read (#2857), and the two standalone
  storage-quota reads (`project_summary` / `quota_list`, #2858).
- **`HarborTargetLike`** (`session.py`) — runtime-checkable Protocol capturing
  the minimum target shape the connector reads: `name`, `host`, `port`,
  `secret_ref`, and `auth_model`. No `sso_realm` field — Harbor sends
  `username:password` as-is; no realm suffix is appended.
- **`HarborCredentialsLoader`** (`session.py`) — async callable type resolving
  a `(target, operator)` pair to `{"username": ..., "password": ...}`. The
  `operator: Operator` carries the dispatched identity so the live loader
  reads the per-target secret under the operator's JWT. Injectable on
  connector construction (`HarborConnector(credentials_loader=...)`) so unit
  and integration tests override the default Vault loader.
- **`load_credentials_from_vault`** (`session.py`) — default loader. Performs a
  live operator-context Vault KV-v2 read of `target.secret_ref` under the
  operator's identity, delegating to the shared `load_basic_credentials` helper
  (G3.9-T2 #941, wired in G3.10-T1 #945). Returns the service-account
  `{"username": ..., "password": ...}` pair.
- **`HARBOR_PRODUCT` / `HARBOR_VERSION` / `HARBOR_IMPL_ID` / `HARBOR_CONNECTOR_ID`**
  (`__init__.py`) — the `Final` connector-triple constants
  (`"harbor"` / `"2.x"` / `"harbor-rest"` / `"harbor-rest-2.x"`), relocated
  from the retired `core_ops` module (#2856) — the same placement the NSX and
  SDDC Manager packages use.
- **`HARBOR_TYPED_OPS`** (`typed_ops.py`) — tuple of 9 `HarborTypedOp` entries,
  the whole audited read core: system info, health, project list/info,
  repository list/info, artifact list/info, robot list. Each carries the
  dot-form `op_id`, the `handler_attr` (the `HarborConnector` method name),
  `summary` / `description`, `parameter_schema`, `response_schema`,
  `group_key`, `tags`, `safety_level="safe"`, `requires_approval=False`, and
  the `llm_instructions` blob (moved verbatim from the retired
  `HARBOR_CORE_OPS`).
- **`HARBOR_TYPED_WHEN_TO_USE_BY_GROUP`** (`typed_ops.py`) — the curated
  `when_to_use` blurb per group (`harbor-system`, `harbor-projects`,
  `harbor-repositories`, `harbor-artifacts`, `harbor-robots`), moved verbatim
  from the retired `HARBOR_CORE_GROUPS`. `register_typed_operation` requires a
  non-empty string whenever a `group_key` is set.
- **`register_harbor_typed_operations`** (`typed_ops.py`) — async lifespan
  registrar that upserts every op in `HARBOR_TYPED_OPS` into
  `endpoint_descriptor` (`source_kind="typed"`). Resolves each op's bound
  method via `getattr(HarborConnector, op.handler_attr)`. Idempotent across
  pod restarts.
- **`typed_reads.py`** — the read op bodies
  (`harbor_about_impl` … `harbor_robot_list_impl`), each an
  `async def f(connector, operator, target, params)` that builds the path
  (percent-encoding each `{var}` segment with `_seg`, mirroring the retired
  generic dispatch), forwards optional query params (`_query`, presence-based
  so a `public=false` is not dropped), and calls `connector._get_json`. Also
  holds `HARBOR_READ_LLM_INSTRUCTIONS` — the per-op `llm_instructions` blobs
  co-located here (for the connector's file-length budget) and consumed by
  `typed_ops.py`.
- **`register_harbor_robot_operations`** (`ops.py`) — async lifespan registrar
  that upserts `harbor.robot.create` and `harbor.robot.delete` (also
  `source_kind="typed"`). Idempotent.
- **`register_harbor_artifact_operations`** (`ops.py`) — async lifespan
  registrar that upserts the standalone typed read `harbor.artifact.vulnerabilities`
  (#2857) into `endpoint_descriptor` (group `harbor-artifacts`, `safety_level=safe`,
  no approval). Same lifespan/idempotency contract as the robot registrar; queued
  separately in `__init__.py`.
- **`register_harbor_project_quota_operations`** (`ops.py`) — async lifespan
  registrar that upserts the wave-2 standalone typed reads
  `harbor.project.summary` and `harbor.quota.list` (#2858) into
  `endpoint_descriptor` (group `harbor-projects`, `safety_level=safe`, no
  approval). Same lifespan/idempotency contract as the robot registrar;
  queued separately in `__init__.py`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.harbor` triggers the module-level
   `register_connector_v2(product="harbor", version="2.x", impl_id="harbor-rest", cls=HarborConnector)`
   call (plus a `("harbor", "", "")` wildcard fallback, #1215), and queues both
   typed-op registrars (`register_harbor_typed_operations` and
   `register_harbor_robot_operations`) via `register_typed_op_registrar`.
3. At lifespan startup `run_typed_op_registrars` invokes the queued registrars,
   landing the 9 read + 2 write descriptor rows before the first dispatch. No
   `meho connector ingest` is required — the read core dispatches on a fresh
   boot with zero catalog state.

### Typed read dispatch

`call_operation` / `dispatch` resolves the target → `HarborConnector`, looks up
the `source_kind="typed"` descriptor for the dot-form `op_id`, resolves the
persisted `handler_ref` (`…connector.HarborConnector.<method>`) to the bound
method, and calls it. The read shim delegates to its `typed_reads` body, which
issues `connector._get_json(target, path, operator=operator, params=query)` on
the connector's own HTTP-Basic-authenticated client. Harbor list endpoints
return a bare JSON array (no `results` / `elements` envelope); a large array is
reduced to a JSONFlux handle by the dispatcher's default reducer.

### Per-target credentials + HTTP Basic auth

Harbor uses HTTP Basic auth — no session cookie or XSRF token is established.
Two account forms are supported:

- **Admin account**: plain username (e.g. `"admin"`).
- **Robot account**: Harbor-formatted username (e.g. `"robot$project+name"`
  for a project-scoped robot or `"robot$name"` for a system-level robot).

Both forms are stored verbatim in Vault under `target.secret_ref`. The
connector passes the stored username through unchanged in the Basic auth header.

1. `HarborConnector.auth_headers(target, operator)` is called. The
   `operator: Operator` is the dispatched identity threaded down from the op
   handler (the operator-context Vault read).
2. `_load_credentials(target, operator)` acquires the per-instance
   `asyncio.Lock`, checks the `_creds_cache` dict (keyed on `target.name`),
   and calls the loader with `(target, operator)` on miss.
3. The loader (default: `load_credentials_from_vault`; injectable in tests)
   returns `{"username": ..., "password": ...}`.
4. The result is cached and a `harbor_credentials_loaded` log event is emitted.
5. `_basic_auth_header(username, password)` returns `"Basic <base64>"`, and
   `auth_headers` returns `{"Authorization": "Basic <base64>"}`.

A raw `401` on a downstream read propagates as `httpx.HTTPStatusError` to the
dispatcher's credential-recovery arm, which evicts the cached credentials via
the connector's public `invalidate_credentials` hook (#2396) and re-dispatches
once.

### fingerprint() / probe()

`fingerprint()` reads `GET /api/v2.0/systeminfo` and splits `harbor_version`
(e.g. `"v2.11.0-abc1234"`) on the first `-` into separate `version` + `build`;
`extras["auth_mode"]` carries the Harbor auth mode. `probe()` reads
`GET /api/v2.0/health` and maps each `component.status` to a single `ok`
boolean (all `"healthy"` → `ok=True`; otherwise `reason` lists the unhealthy
components). Unlike the SDDC Manager / NSX precedents that delegate `probe()`
to `fingerprint()`, Harbor's health endpoint is the purpose-built reachability
surface covering subsystem state `systeminfo` does not expose. `harbor.about`
and `harbor.health` expose these same two reads as operator-callable typed ops.

### robot_create / robot_delete

`robot_create` (op `harbor.robot.create`) is classified `credential_mint` by
`classify_op` in `broadcast/events.py` — the broadcast collapses to
aggregate-only so the minted secret never appears in the SSE stream — and is
registered `requires_approval=True` (#147): minting a robot credential is
privilege issuance, so a human `tenant_admin` dispatch **parks** at
`awaiting_approval` and a second operator must approve it before the mint
executes. `robot_delete` (op `harbor.robot.delete`) stays ungated — a
`caution`-class `write` that revokes access rather than minting a credential
(mirroring the bind9 #129 precedent). Both forward the dispatched `operator`
through `auth_headers` → `_load_credentials` for the operator-context Vault
read and use non-retried HTTP calls (Harbor write endpoints are non-idempotent).

### artifact_vulnerabilities(operator, target, params)

Typed op handler for `harbor.artifact.vulnerabilities` (#2857) — the per-CVE
vulnerability list behind `harbor.artifact.info`'s `scan_overview` (which
carries severity **counts** only). Classified `read` (`classify_op`;
`.vulnerabilities` is a `_READ_SUFFIXES` entry, so the broadcast sensitivity
matches its `harbor.artifact.info` sibling rather than falling through to the
full-detail `other` class). `safety_level=safe`, no approval.

Like the robot handlers, the signature carries `operator: Operator` so the
dispatched operator threads in and is forwarded to `auth_headers` →
`_load_credentials` for the operator-context Vault read.

1. Percent-encodes `project_name`, `repository_name`, `reference` with
   `quote(value, safe="")` — the OpenAPI simple-style encoding the generic
   dispatcher uses for `harbor.artifact.info`, so a nested `repository_name`
   slash (`team/nginx` → `team%2Fnginx`) and a `sha256:` reference colon
   (`→ %3A`) never leak path structure.
2. Calls `_request_json(target, "GET", path, operator=operator,
   extra_headers={"X-Accept-Vulnerabilities": "application/vnd.security.vulnerability.report; version=1.1"})`.
   `_request_json` (not `_get_json`) is used because `_get_json` does not
   forward per-call headers; GET stays idempotent, so the tenacity retry still
   applies.
3. Unwraps the MIME-keyed `HarborVulnerabilityReport` (Harbor keys the report
   by the resolved media type, mirroring `scan_overview`; a bare already-unwrapped
   report is tolerated) and projects each native `VulnerabilityItem` to
   `{id, package, version, fix_version, severity, description, links}`. Field
   names follow the pluggable-scanner-spec v1.1 (`id` / `fix_version` /
   `description`), **not** the Harbor Security-Hub `VulnerabilityItem` spellings
   (`cve_id` / `fixed_version` / `desc`).
4. Returns `{severity, scanner, generated_at, vulnerabilities: [...]}`.
   `vulnerabilities` is the single set-shaped field, so the dispatcher's
   JSONFlux reducer materialises it into a result handle when a real image's
   CVE list crosses the ~50-row / 4 KB threshold (postulate 6). A named-CVE
   lookup against that handle is a `result_query`
   `SELECT ... WHERE id = 'CVE-…'` away. A never-scanned artifact returns
   Harbor 404 → `httpx.HTTPStatusError` → `connector_error`.

### project_summary(operator, target, params)

Typed op handler for `harbor.project.summary` (#2858) — a project's
storage-quota occupancy. Classified `read` (`classify_op`; `.summary` is a
`_READ_SUFFIXES` entry, so its broadcast sensitivity matches the
`harbor.project.info` sibling rather than falling through to the full-detail
`other` class — the same edit also reclassifies
`vmware.composite.performance.summary` `other`→`read`, a correctness
improvement; neither class is sensitive, so redaction is unchanged).
`safety_level=safe`, no approval. Like the robot handlers, the `operator`
signature threads the dispatched operator to `auth_headers` →
`_load_credentials` for the operator-context Vault read.

1. Percent-encodes `project_name` with `quote(value, safe="")`.
2. Calls `_request_json(target, "GET",
   "/api/v2.0/projects/{name}/summary", operator=operator,
   extra_headers={"X-Is-Resource-Name": "true"})`. `_request_json` (not
   `_get_json`) forwards the per-call header; the header makes Harbor resolve
   the path segment as a project *name* even when it looks numeric (the vendor
   default treats a numeric segment as an id). GET stays idempotent, so the
   tenacity retry still applies.
3. Projects the native `ProjectSummary` to
   `{repo_count, quota: {hard, used}, member_counts}`. `quota.hard`/`quota.used`
   are `ResourceList` maps (`storage` in bytes; hard `-1` = unlimited); the
   proxy-only `registry` field is dropped. **This is the quota
   `harbor.project.info` does not carry** — the vendor `Project` object has no
   `quota` field.

### quota_list(operator, target, params)

Typed op handler for `harbor.quota.list` (#2858) — fleet-wide project storage
quotas, the "which projects are near quota" read. Classified `read` (`.list`
suffix), `safety_level=safe`, no approval. `operator` threads the same way.

1. Builds the query `{reference: "project", sort, page, page_size}`.
   `reference=project` is fixed (project quotas are the only reference type in
   the stable Harbor 2.x line); `sort` defaults to `-used.storage` (fullest
   projects first).
2. Calls `_get_json(target, "/api/v2.0/quotas", operator=operator,
   params=query)` — idempotent GET with query params, no per-call header.
3. Projects each native `Quota` to `{id, ref, hard, used}` and returns a
   **bare list**. `ref` is the project reference (`{id, name, owner_name}`);
   `hard`/`used` are `ResourceList` maps (`storage` in bytes). The dispatcher's
   JSONFlux reducer materialises the list into a result handle when the fleet
   crosses the ~50-row / 4 KB threshold (postulate 6); a "projects over N bytes
   used" filter is a `result_query` away.

### execute() shim

`execute()` synthesises a system `Operator` and delegates to
`meho_backplane.operations.dispatch(connector_id="harbor-rest-2.x", ...)`.
Post-G0.6 callers (CLI verbs, MCP `call_operation`, `/api/v1/operations/call`)
construct a real `Operator` and call `dispatch` directly — they bypass this shim.

## Dependencies

- **httpx 0.28.1** — async HTTP client with per-target pooling and retry decorator.
  `_get_json` (retried GET) backs the 9 reads; `_post_json` (non-retried POST)
  backs `robot_create`; `_http_client` + `client.request("DELETE", …)` backs
  `robot_delete`.
- **tenacity 9.1.4** — retry logic for idempotent GET requests (3 retries,
  exponential backoff, 5xx + connection errors only). Robot lifecycle writes
  bypass tenacity intentionally — those endpoints are non-idempotent.
- **structlog** — structured logging for credential load + registrar events.
- **`meho_backplane.connectors.adapters.http.HttpConnector`** — base class
  providing `_get_json`, `_post_json`, `_http_client`, and `aclose`.
- **`meho_backplane.connectors.schemas`** — `FingerprintResult`, `ProbeResult`,
  `OperationResult`, `AuthModel`.
- **`meho_backplane.operations.typed_register`** — `register_typed_operation`,
  `register_typed_op_registrar`, `run_typed_op_registrars`, `derive_handler_ref`.
- **`meho_backplane.broadcast.events`** — `classify_op` returns `read` for the
  9 read-core ops and `harbor.artifact.vulnerabilities` (via the `.about` /
  `.health` / `.list` / `.info` read suffixes plus `.vulnerabilities`, #2857)
  and `credential_mint` for `harbor.robot.create`.

## The read-only core ops

All register under `connector_id="harbor-rest-2.x"` as `source_kind="typed"`
and dispatch on a fresh boot with zero catalog ingest. The first 9 rows below
are the audited read core (`HARBOR_TYPED_OPS`, via
`register_harbor_typed_operations`); the `harbor.artifact.vulnerabilities` row
(#2857) is a **standalone typed read** registered separately in `ops.py` via
`register_harbor_artifact_operations`, and the `harbor.project.summary` /
`harbor.quota.list` rows (#2858) are **standalone typed reads** registered via
`register_harbor_project_quota_operations` — all read-only like the core, but
not one of the 9.

| Op id | Group | Vendor path (Harbor 2.x) |
|---|---|---|
| `harbor.about` | `harbor-system` | `GET /api/v2.0/systeminfo` |
| `harbor.health` | `harbor-system` | `GET /api/v2.0/health` |
| `harbor.project.list` | `harbor-projects` | `GET /api/v2.0/projects` |
| `harbor.project.info` | `harbor-projects` | `GET /api/v2.0/projects/{project_name}` |
| `harbor.repository.list` | `harbor-repositories` | `GET /api/v2.0/projects/{project_name}/repositories` |
| `harbor.repository.info` | `harbor-repositories` | `GET /api/v2.0/projects/{project_name}/repositories/{repository_name}` |
| `harbor.artifact.list` | `harbor-artifacts` | `GET …/repositories/{repository_name}/artifacts` |
| `harbor.artifact.info` | `harbor-artifacts` | `GET …/artifacts/{reference}` |
| `harbor.robot.list` | `harbor-robots` | `GET /api/v2.0/robots` |
| `harbor.artifact.vulnerabilities` *(standalone typed read, #2857)* | `harbor-artifacts` | `GET …/artifacts/{reference}/additions/vulnerabilities` |
| `harbor.project.summary` *(standalone typed read, #2858)* | `harbor-projects` | `GET /api/v2.0/projects/{project_name}/summary` |
| `harbor.quota.list` *(standalone typed read, #2858)* | `harbor-projects` | `GET /api/v2.0/quotas?reference=project` |

**Quota lives on the summary endpoint, not on `Project`**: `harbor.project.info`
(`GET /projects/{name}`) returns the vendor `Project` object, which has no
`quota` and no `chart_count` field. Per-project storage occupancy
(`quota.hard`/`quota.used` in bytes) comes from `harbor.project.summary`
(`GET /projects/{name}/summary`); the fleet-wide "which projects are near
quota" answer comes from `harbor.quota.list` (`GET /quotas`).

**Robot id + secret invariant**: `harbor.robot.list` returns each robot's
numeric `id` (needed for `harbor.robot.delete`) and never a `secret` — Harbor
only exposes secrets in the `POST` create response, and the read handler
returns Harbor's list payload verbatim. The unit and acceptance tests assert
this invariant explicitly.

## Tests

- `tests/test_connectors_harbor_typed_reads.py` — unit tests (SQLite): each read
  dispatches typed with zero catalog state, path interpolation + query
  forwarding, `source_kind="typed"`, the robot id/secret invariant,
  `classify_op == "read"`, and the registration-shape invariants (safe,
  no-approval, read-only tag, `llm_instructions` canonical keys, no write op).
- `tests/acceptance/_harbor_canary_fixtures.py` — shared fixtures: runs the
  typed registrar, seeds a `Target`, respx-mocks the Harbor REST surface, and
  stubs the credentials loader.
- `tests/acceptance/test_g35_harbor_dispatch_smoke.py` — parametrised smoke over
  all 9 typed op ids with **no ingest fixture**; asserts `status='ok'`.
- `tests/acceptance/test_g35_harbor_jsonflux_force_handle.py` — JSONFlux
  force-handle seam test using `harbor.artifact.list` (bare JSON array shape,
  distinct from NSX/SDDC's pagination envelopes).
- `tests/test_connectors_harbor_cve.py` — unit tests for
  `harbor.artifact.vulnerabilities` (#2857): native-report projection (CVE id
  field present), the `X-Accept-Vulnerabilities` header, path-segment encoding
  parity with `harbor.artifact.info`, MIME-keyed / bare envelope unwrap, empty
  and 404 paths, `dispatch_typed` operator threading, and the `read`
  classification.
- `tests/test_connectors_harbor_quota.py` — unit tests for
  `harbor.project.summary` and `harbor.quota.list` (#2858): quota-byte
  projection, the `X-Is-Resource-Name` header, the `reference=project` +
  `-used.storage` default query, `dispatch_typed` operator threading, `read`
  classification, and the `harbor.project.info` overpromise fix (no `quota` /
  `chart_count` in the op's `llm_instructions` or the `Project` fixtures).
- `tests/integration/test_connectors_harbor_container.py` — env-gated real
  Harbor `v2.11.0` stack E2E: `harbor.about` + `harbor.robot.list` reads and
  the robot create/delete four-eyes flow, all `source_kind="typed"`.

## Known issues

- `load_credentials_from_vault` performs the live operator-context Vault read
  (G3.10-T1 #945) via the shared `load_basic_credentials` helper; all ops read
  the per-target service-account credential under the dispatched operator's
  identity. Tests can inject a custom loader.
- `harbor.robot.create` grants push + pull access on the named project only.
  System-level robot creation (`POST /api/v2.0/robots`) is out of scope.
- Robot secret rotation / refresh is out of scope — tracked as a follow-up.

## References

- Issues: #619 (G3.5-T7 skeleton), #621 (G3.5-T9 robot lifecycle +
  credential_mint classifier), #622 (G3.5-T10 CLI verbs + real-container E2E +
  harbor-onboarding.md), **#2856 (typed read-core conversion)**,
  #2857 (wave-2 CVE-detail read `harbor.artifact.vulnerabilities`),
  #2858 (wave-2 storage-quota reads `harbor.project.summary` /
  `harbor.quota.list` + `harbor.project.info` overpromise fix)
- Precedent: Task #2358 / Goal #2247 — the NSX (#2302) and SDDC Manager (#2306)
  typed-read conversions this task mirrors
- Initiative: #368 (G3.5 tier-2 batch), #2833 (connector read-op coverage wave 2)
- Harbor `getVulnerabilitiesAddition` + `acceptVulnerabilities` header:
  https://github.com/goharbor/harbor/blob/v2.11.0/api/v2.0/swagger.yaml
- Native report `VulnerabilityItem` shape (`id` / `fix_version` /
  `description`): goharbor/pluggable-scanner-spec v1.1
  (`application/vnd.security.vulnerability.report; version=1.1`)
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Broadcast classifier: `backend/src/meho_backplane/broadcast/events.py` (`classify_op`)
- Handler resolution: `backend/src/meho_backplane/operations/_handler_resolve.py`
  (`import_handler`, `is_unbound_method`)
- Harbor 2.x API: https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/
  (paths confirmed against `goharbor/harbor` tag `v2.11.0`, `api/v2.0/swagger.yaml`)
- Precedents: `connectors/nsx/` (typed_ops + typed_reads),
  `connectors/sddc_manager/` (typed reads + Basic-auth pattern),
  `connectors/vault/ops.py` (typed op registration)

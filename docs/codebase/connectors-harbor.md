# Connector: harbor (Harbor 2.x)

## Overview

The `harbor` connector is the hand-rolled `HttpConnector` subclass that
dispatches Harbor REST operations under the
`(product="harbor", version="2.x", impl_id="harbor-rest")` registry triple.
G3.5-T7 (#619) shipped the skeleton — HTTP Basic auth, fingerprint, probe, and
the G0.6 dispatch shim. G3.5-T8 (#620) ships `core_ops.py` with the 9
operator-reviewed read-only ops, the curated group/op metadata, and the
acceptance test suite (dispatch smoke + JSONFlux force-handle). Robot lifecycle
(create/delete) + G6 credential_mint classifier arrive in G3.5-T9 (#621).
CLI verbs + MCP review + real-container E2E arrive in G3.5-T10 (#622).

Source: `backend/src/meho_backplane/connectors/harbor/`.

## Key types

- **`HarborConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="harbor"`, `version="2.x"`,
  `impl_id="harbor-rest"`, `supported_version_range=">=2.0,<3.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim (priority=0) defensively if both somehow register for the same
  triple.
- **`HarborTargetLike`** (`session.py`) — runtime-checkable Protocol capturing
  the minimum target shape the connector reads: `name`, `host`, `port`,
  `secret_ref`, and `auth_model`. No `sso_realm` field — Harbor sends
  `username:password` as-is; no realm suffix is appended. Replaced by the
  concrete `Target` model once G0.3 (#224) lands.
- **`HarborCredentialsLoader`** (`session.py`) — async callable type resolving
  a target to `{"username": ..., "password": ...}`. Injectable on connector
  construction (`HarborConnector(credentials_loader=...)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the default
  Vault loader.
- **`load_credentials_from_vault`** (`session.py`) — default loader, stubbed
  `NotImplementedError` until G0.3 lands the operator-context Vault read path.
- **`HARBOR_CORE_OPS`** (`core_ops.py`) — tuple of 9 `HarborCoreOp` entries
  describing the operator-reviewed read-only op subset enabled at v0.2: system
  info, health, project list/info, repository list/info, artifact list/info,
  robot list. Each entry carries the `op_id` (`METHOD:/path` form), the
  curated `group_key`, and the `llm_instructions` blob.
- **`HARBOR_CORE_GROUPS`** (`core_ops.py`) — tuple of 5 `HarborCoreGroup`
  entries: `harbor-system`, `harbor-projects`, `harbor-repositories`,
  `harbor-artifacts`, `harbor-robots`. Each carries the operator-reviewed
  `when_to_use` hint the agent reads via `list_operation_groups`.
- **`HARBOR_PATH_RULES`** (`core_ops.py`) — ordered tuple of
  `(prefix, group_key)` pairs used by `classify_harbor_op`. Order is
  load-bearing: artifact paths precede repository paths which precede project
  paths — each is a prefix of the next.
- **`classify_harbor_op(op_id)`** (`core_ops.py`) — returns the curated
  `group_key` for a Harbor op_id string, or `"none"` for uncurated paths.
- **`apply_harbor_core_curation(review_service, tenant_id)`** (`core_ops.py`)
  — operator-review-time substrate call; enables the 9 core ops, disables
  non-core ops in curated groups via the audit-log-driven override exclusion,
  and lands the reviewed `when_to_use` / `llm_instructions` metadata.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.harbor` triggers the module-level
   `register_connector_v2(product="harbor", version="2.x", impl_id="harbor-rest", cls=HarborConnector)`
   call.
3. The registry's v2 table now resolves `("harbor", "2.x", "harbor-rest")`
   to `HarborConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`, once #408's pipeline lands in main)
   no-ops on subsequent ingests against the same triple.

### Per-target credentials + HTTP Basic auth

Harbor uses HTTP Basic auth — no session cookie or XSRF token is established.
Two account forms are supported:

- **Admin account**: plain username (e.g. `"admin"`).
- **Robot account**: Harbor-formatted username (e.g. `"robot$project+name"`
  for a project-scoped robot or `"robot$name"` for a system-level robot).

Both forms are stored verbatim in Vault under `target.secret_ref`. The
connector passes the stored username through unchanged in the Basic auth header.

1. `HarborConnector.auth_headers(target)` is called.
2. `_load_credentials(target)` acquires the per-instance `asyncio.Lock`,
   checks the `_creds_cache` dict (keyed on `target.name`), and calls the
   loader on miss.
3. The loader (default: `load_credentials_from_vault`, injected in tests)
   returns `{"username": ..., "password": ...}`.
4. The result is cached under `target.name` and a `harbor_credentials_loaded`
   log event is emitted.
5. `_basic_auth_header(username, password)` returns `"Basic <base64>"`.
6. `auth_headers` returns `{"Authorization": "Basic <base64>"}`.

### fingerprint()

`GET /api/v2.0/systeminfo` → `GeneralInfo` object. The `harbor_version` field
(e.g. `"v2.11.0-abc1234"`) is split on the first `-` to extract separate
`version` (`"v2.11.0"`) and `build` (`"abc1234"`) values. `extras["auth_mode"]`
carries the Harbor auth mode (`"db_auth"`, `"ldap_auth"`, `"oidc_auth"`).

On transport or status error, returns `FingerprintResult(reachable=False,
extras={"error": "<ExcType>: <message>"})`.

### probe()

`GET /api/v2.0/health` → `OverallHealthStatus`. The connector checks each
`component.status` field; if all are `"healthy"`, returns `ProbeResult(ok=True)`.
If any component is unhealthy, `reason` lists the unhealthy component names.

This differs from the SDDC Manager / NSX precedents that delegate to
`fingerprint()`. Harbor's health endpoint is purpose-built for reachability
checks and covers subsystem state (DB, redis, registry, jobservice) that
`systeminfo` does not expose.

### execute() shim

`execute()` synthesises a system `Operator` with `sub="system:harbor-rest-connector-shim"`
and delegates to `meho_backplane.operations.dispatch(connector_id="harbor-rest-2.x", ...)`.
Post-G0.6 callers (CLI verbs, MCP `call_operation`, `/api/v1/operations/call`)
construct a real `Operator` and call `dispatch` directly — they bypass this shim.

## Dependencies

- **httpx 0.28.1** — async HTTP client with per-target pooling and retry decorator.
- **tenacity 9.1.4** — retry logic for idempotent GET requests (3 retries,
  exponential backoff, 5xx + connection errors only).
- **structlog** — structured logging for credential load events.
- **`meho_backplane.connectors.adapters.http.HttpConnector`** — base class
  providing `_get_json`, `_post_json`, `_http_client`, and `aclose`.
- **`meho_backplane.connectors.schemas`** — `FingerprintResult`, `ProbeResult`,
  `OperationResult`, `AuthModel`.

## The 9 read-only v0.2 core ops

All paths under `connector_id="harbor-rest-2.x"`. Op ids use `METHOD:/path`
form matching `source_kind='ingested'` rows in `endpoint_descriptor`.

| Op id | Group | Operator label |
|---|---|---|
| `GET:/api/v2.0/systeminfo` | `harbor-system` | `harbor.about` |
| `GET:/api/v2.0/health` | `harbor-system` | `harbor.health` |
| `GET:/api/v2.0/projects` | `harbor-projects` | `harbor.project.list` |
| `GET:/api/v2.0/projects/{project_name}` | `harbor-projects` | `harbor.project.info` |
| `GET:/api/v2.0/projects/{project_name}/repositories` | `harbor-repositories` | `harbor.repository.list` |
| `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}` | `harbor-repositories` | `harbor.repository.info` |
| `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts` | `harbor-artifacts` | `harbor.artifact.list` |
| `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}` | `harbor-artifacts` | `harbor.artifact.info` |
| `GET:/api/v2.0/robots` | `harbor-robots` | `harbor.robot.list` |

**Robot secret invariant**: `GET /api/v2.0/robots` never returns a `secret`
field — Harbor only exposes secrets in the `POST` create response. The
acceptance fixtures and unit tests assert this invariant explicitly.

## Acceptance tests

- `tests/test_connectors_harbor_core_ops.py` — unit tests for
  `classify_harbor_op`, `apply_harbor_core_curation`, the robot-secret
  invariant, and `llm_instructions` / `when_to_use` non-emptiness.
- `tests/acceptance/_harbor_canary_fixtures.py` — shared fixtures: seeded
  descriptor rows, respx-mocked Harbor REST surface, stub credentials loader.
- `tests/acceptance/test_g35_harbor_dispatch_smoke.py` — parametrised smoke
  over all 9 op ids; asserts `status='ok'` for each dispatch.
- `tests/acceptance/test_g35_harbor_jsonflux_force_handle.py` — JSONFlux
  force-handle seam test using `harbor.artifact.list` (plain JSON array
  response shape, distinct from NSX/SDDC's pagination envelopes).

## Known issues

- `load_credentials_from_vault` is a `NotImplementedError` stub until G0.3
  (#224) lands the operator-context Vault read path. Tests inject a custom
  loader.
- Real-container integration tests (against `goharbor/harbor-core:v2.11`) are
  out of scope for this Task; they arrive in #622.
- Full G0.7 spec-canary ingest (live Harbor OpenAPI spec through
  `IngestionPipelineService`) is deferred to #622 when the spec-shelf is
  wired to the meho-runners pool.

## References

- Issues: #619 (G3.5-T7 skeleton), #620 (G3.5-T8 read-ops, this task)
- Successor tasks: #621 (robot lifecycle), #622 (CLI/MCP/E2E)
- Initiative: #368 (G3.5 tier-2 batch)
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Harbor 2.x API: https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/
- Precedents: `connectors/sddc_manager/` (Basic auth + core_ops pattern),
  `connectors/nsx/` (probe pattern + acceptance fixtures)

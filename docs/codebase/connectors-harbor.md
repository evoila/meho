# Connector: harbor (Harbor 2.x)

## Overview

The `harbor` connector is the hand-rolled `HttpConnector` subclass that
dispatches Harbor REST operations under the
`(product="harbor", version="2.x", impl_id="harbor-rest")` registry triple.
G3.5-T7 (#619) shipped the skeleton — HTTP Basic auth, fingerprint, probe, and
the G0.6 dispatch shim. G3.5-T8 (#620) ships `core_ops.py` with the 9
operator-reviewed read-only ops, the curated group/op metadata, and the
acceptance test suite (dispatch smoke + JSONFlux force-handle). G3.5-T9 (#621)
adds robot lifecycle typed ops (`harbor.robot.create` / `harbor.robot.delete`)
and the `credential_mint` G6 broadcast classifier.
G3.5-T10 (#622) adds the `meho harbor …` CLI verb tree
(`cli/internal/cmd/harbor/`), the real-container E2E test against
`goharbor/harbor-core:v2.11.0`, and `docs/cross-repo/harbor-onboarding.md`.

Source: `backend/src/meho_backplane/connectors/harbor/`.

## Key types

- **`HarborConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="harbor"`, `version="2.x"`,
  `impl_id="harbor-rest"`, `supported_version_range=">=2.0,<3.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim (priority=0) defensively if both somehow register for the same
  triple. Handler methods `robot_create` / `robot_delete` are bound-method
  handlers the dispatcher resolves and binds to the per-process connector
  instance at dispatch time.
- **`HarborTargetLike`** (`session.py`) — runtime-checkable Protocol capturing
  the minimum target shape the connector reads: `name`, `host`, `port`,
  `secret_ref`, and `auth_model`. No `sso_realm` field — Harbor sends
  `username:password` as-is; no realm suffix is appended. Replaced by the
  concrete `Target` model once G0.3 (#224) lands.
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
- **`register_harbor_robot_operations`** (`ops.py`) — async lifespan registrar
  that upserts `harbor.robot.create` and `harbor.robot.delete` into
  `endpoint_descriptor`. Called by the lifespan via `run_typed_op_registrars`.
  Idempotent.

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

1. `HarborConnector.auth_headers(target, operator)` is called. The
   `operator: Operator` is the dispatched identity threaded down from the op
   handler (the operator-context Vault read, #945 for read ops, #984 for the
   robot lifecycle write ops).
2. `_load_credentials(target, operator)` acquires the per-instance
   `asyncio.Lock`, checks the `_creds_cache` dict (keyed on `target.name`),
   and calls the loader with `(target, operator)` on miss.
3. The loader (default: `load_credentials_from_vault`, which reads the secret
   under the operator's identity; injectable in tests) returns
   `{"username": ..., "password": ...}`.
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

### robot_create(operator, target, params)

Typed op handler for `harbor.robot.create`. Classified `credential_mint` by
`classify_op` in `broadcast/events.py` — the broadcast collapses to aggregate-only
so the minted secret never appears in the SSE stream.

The signature carries `operator: Operator` so `dispatch_typed`
(`operations/_branches.py`, name-keyed operator threading) passes the
dispatched operator into the handler. The operator is forwarded to `_post_json`
→ `auth_headers` → `_load_credentials` so the per-target service-account
credential is read under the operator's identity (the operator-context Vault
read, #984). The operator's JWT authenticates the credential read, not the
Harbor request itself.

1. Validates `name`, `project`, `duration` from `params`.
2. Calls `_post_json(target, "/api/v2.0/robots", operator=operator, json=body)`.
   Non-retried — Harbor's create endpoint is non-idempotent.
3. Returns `{id, name, secret}` extracted from Harbor's 201 response.
   The `secret` is the minted credential, returned only on creation.

The `permissions` body grants push + pull access on the named project
(`level="project"`). System-level robot creation is out of scope for this Task.

### robot_delete(operator, target, params)

Typed op handler for `harbor.robot.delete`. Classified `write` (suffix-based).
No secret material in the response.

Like `robot_create`, the signature carries `operator: Operator` so the
dispatched operator threads in and is forwarded to `auth_headers` →
`_load_credentials` for the operator-context Vault read (#984).

1. Validates `project`, `id` from `params`.
2. Acquires the pooled httpx client via `_http_client(target)`, computes the
   Basic-auth header with `auth_headers(target, operator)`, and calls
   `client.request("DELETE", "/api/v2.0/robots/{id}", headers=auth_headers)`.
   Direct client call (no `_delete_json` helper on `HttpConnector`) — non-retried.
3. Calls `resp.raise_for_status()` — propagates `httpx.HTTPStatusError` on 4xx/5xx.
4. Returns `{id, deleted: True}` (Harbor returns HTTP 200 with empty body;
   the `id` echo is synthesized for a useful agent-facing result).

### execute() shim

`execute()` synthesises a system `Operator` with `sub="system:harbor-rest-connector-shim"`
and delegates to `meho_backplane.operations.dispatch(connector_id="harbor-rest-2.x", ...)`.
Post-G0.6 callers (CLI verbs, MCP `call_operation`, `/api/v1/operations/call`)
construct a real `Operator` and call `dispatch` directly — they bypass this shim.

## Dependencies

- **httpx 0.28.1** — async HTTP client with per-target pooling and retry decorator.
  `_post_json` is used for `robot_create` (non-retried POST);
  `_http_client` + `client.request("DELETE", ...)` for `robot_delete`.
- **tenacity 9.1.4** — retry logic for idempotent GET requests (3 retries,
  exponential backoff, 5xx + connection errors only). Robot lifecycle ops bypass
  tenacity intentionally — write endpoints are non-idempotent.
- **structlog** — structured logging for credential load events.
- **`meho_backplane.connectors.adapters.http.HttpConnector`** — base class
  providing `_get_json`, `_post_json`, `_http_client`, and `aclose`.
- **`meho_backplane.connectors.schemas`** — `FingerprintResult`, `ProbeResult`,
  `OperationResult`, `AuthModel`.
- **`meho_backplane.broadcast.events`** — `classify_op` returns `credential_mint`
  for `harbor.robot.create`; `redact_payload` treats `credential_mint` as
  aggregate-only (same branch as `credential_read`).

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

- `load_credentials_from_vault` performs the live operator-context Vault
  read (G3.10-T1 #945) via the shared `load_basic_credentials` helper; all
  ops — read core (#945) and the robot lifecycle write ops (#984) — read the
  per-target service-account credential under the dispatched operator's
  identity. Tests can still inject a custom loader, but the robot-op suite
  exercises the live read against the in-process Vault fake rather than a
  masking stub.
- `harbor.robot.create` grants push + pull access on the named project only.
  System-level robot creation (`POST /api/v2.0/robots`) is out of scope for this Task.
- Robot secret rotation / refresh is out of scope — tracked as a follow-up.
- Full G0.7 spec-canary ingest (live Harbor OpenAPI spec through
  `IngestionPipelineService`) remains deferred; wired to the meho-runners
  pool as a follow-up after the spec-shelf ships.

## References

- Issues: #619 (G3.5-T7 skeleton), #620 (G3.5-T8 read-ops curation),
  #621 (G3.5-T9 robot lifecycle + credential_mint classifier),
  #622 (G3.5-T10 CLI verbs + real-container E2E + harbor-onboarding.md)
- Initiative: #368 (G3.5 tier-2 batch)
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Broadcast classifier: `backend/src/meho_backplane/broadcast/events.py` (`classify_op`, `redact_payload`)
- Harbor 2.x API: https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/
- Precedents: `connectors/sddc_manager/` (Basic auth + core_ops pattern),
  `connectors/nsx/` (probe pattern + acceptance fixtures),
  `connectors/vault/ops.py` (typed op registration),
  `connectors/bind9/` (bound-method handlers)

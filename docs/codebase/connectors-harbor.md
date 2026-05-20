# Connector: harbor (Harbor 2.x)

## Overview

The `harbor` connector is the hand-rolled `HttpConnector` subclass that
dispatches Harbor REST operations under the
`(product="harbor", version="2.x", impl_id="harbor-rest")` registry triple.
G3.5-T7 (#619) shipped the skeleton — HTTP Basic auth, fingerprint, probe, and
the G0.6 dispatch shim. G3.5-T8 (#620) adds spec ingestion + operator-review
curation + ~8 read-only ops (project/repo/artifact lists). Robot lifecycle
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

## Known issues

- `load_credentials_from_vault` is a `NotImplementedError` stub until G0.3
  (#224) lands the operator-context Vault read path. Tests inject a custom
  loader.
- Operations are not yet registered — `execute()` will produce "unknown
  operation" at the dispatcher layer until G0.7 spec ingestion lands in #620.
- Real-container integration tests (against `goharbor/harbor-core:v2.11`) are
  out of scope for this Task; they arrive in #622.

## References

- Issue: #619 (G3.5-T7)
- Initiative: #368 (G3.5 tier-2 batch)
- Successor tasks: #620 (read-ops ingestion), #621 (robot lifecycle), #622 (CLI/MCP/E2E)
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Harbor 2.x API: https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/
- Precedents: `connectors/sddc_manager/` (Basic auth), `connectors/nsx/` (probe pattern)

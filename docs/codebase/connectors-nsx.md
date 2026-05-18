# Connector: nsx (NSX 4.x)

## Overview

The `nsx` connector is the hand-rolled `HttpConnector` subclass that
will dispatch ingested NSX REST operations under the
`(product="nsx", version="4.2", impl_id="nsx-rest")` registry triple.
G3.5-T1 (#613) ships only the skeleton -- session-cookie / XSRF auth,
fingerprint, probe, and the G0.6 dispatch shim. Operations arrive in
G3.5-T2 (#614) via G0.7 spec ingestion against `nsx-4.2/policy.yaml`
+ `nsx-4.2/manager.yaml`; CLI verbs + MCP review + recorded-fixture
E2E arrive in G3.5-T3 (#615).

Source: `backend/src/meho_backplane/connectors/nsx/`.

## Key types

- **`NsxConnector`** (`connector.py`) -- `HttpConnector` subclass.
  Class attributes: `product="nsx"`, `version="4.2"`,
  `impl_id="nsx-rest"`, `supported_version_range=">=4.0,<5.0"`,
  `priority=1`. The priority outranks a future
  `GenericRestConnector` auto-shim (priority=0) defensively if both
  somehow register for the same triple.
- **`NsxTargetLike`** (`session.py`) -- runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the
  concrete `Target` model once G0.3 (#224) lands; the model
  satisfies the Protocol structurally without code edits here.
- **`NsxSessionLoader`** (`session.py`) -- async callable type
  resolving a target to `{"username": ..., "password": ...}`.
  Injectable on connector construction
  (`NsxConnector(session_loader=...)`) so unit tests, integration
  tests, and pre-G0.3 production deploys override the default Vault
  loader.
- **`load_session_credentials_from_vault`** (`session.py`) -- default
  loader, stubbed `NotImplementedError` until G0.3 lands the
  operator-context Vault read path. Mirrors the
  `load_session_credentials_from_vault` shape in
  `connectors/vmware_rest/`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.nsx` triggers the
   module-level
   `register_connector_v2(product="nsx", version="4.2", impl_id="nsx-rest", cls=NsxConnector)`
   call.
3. The registry's v2 table now resolves `("nsx", "4.2", "nsx-rest")`
   to `NsxConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.

### Per-target session

NSX auth diverges from the vSphere precedent: the canonical FQDN
behind the VCF 9 envoy proxy rejects HTTP Basic, and the session
endpoint accepts only form-encoded credentials. The flow:

1. `NsxConnector.auth_headers(target)` is called for the first time
   against `target`.
2. `_session_token(target)` acquires the per-instance `asyncio.Lock`,
   checks the `self._session_tokens` cache (keyed on `target.name`),
   misses, calls the injected `session_loader(target)` -> resolves
   to `{"username": ..., "password": ...}`.
3. `_session_token` POSTs to `/api/session/create` with
   `data={"j_username": ..., "j_password": ...}` -- `httpx 0.28.x`'s
   `client.post(url, data=<dict>)` sends
   `application/x-www-form-urlencoded`.
4. The response's `Set-Cookie: JSESSIONID=...` header is captured
   into the per-target `httpx.AsyncClient.cookies` jar automatically
   (`httpx/_client.py:1737` -- `self.cookies.extract_cookies(response)`).
5. The response's `X-XSRF-TOKEN` header is captured into
   `self._session_tokens[target.name]`.
6. `auth_headers()` returns `{"X-XSRF-TOKEN": <cached>}`. The
   `JSESSIONID` cookie travels via the client jar; subsequent
   requests through the same per-target client get both
   transparently.

### 401 -> re-login -> retry-once

`_get_json_with_session_retry(target, path)` wraps the inherited
`HttpConnector._get_json`. The inherited method carries tenacity's
`@retry(retry_if_exception(_retryable))` decorator that excludes 4xx
responses, so a 401 propagates cleanly to the wrapper.

1. First call to `_get_json` succeeds -> return.
2. First call raises `httpx.HTTPStatusError(401)` ->
   `_invalidate_session(target)` acquires the lock, drops the cached
   XSRF token, and clears the per-target client cookie jar.
3. Second call to `_get_json` re-establishes the session via
   `auth_headers -> _session_token` (cache-miss path) and re-tries
   the GET.
4. Second call succeeds -> return; second call raises 401 ->
   wrapper raises
   `RuntimeError("nsx session re-login failed for target ...")`.
5. Any non-401 status error propagates untouched -- relogin would
   mask transient backend failures.

The single retry posture (not a loop) matches the consumer wrapper
in `scripts/nsx.sh` so a misconfigured credential pair fails fast
instead of hammering NSX's audit log.

### Fingerprint + probe

- `fingerprint(target)` issues `GET /api/v1/node` through
  `_get_json_with_session_retry`. On success: returns
  `FingerprintResult(vendor="vmware", product="nsx",
  version=<node_version>, build=<kernel_version>, reachable=True,
  extras={"node_uuid", "hostname", "external_id"})`. On transport,
  HTTP-status, or session-establish failure: returns
  `reachable=False` with `extras["error"] = "<ExcType>: <message>"`.
- `probe(target)` delegates to `fingerprint`. The issue body
  permits an alternative `GET /api/v1/cluster/status` call; the
  delegation path is chosen for parity with `VmwareRestConnector`
  -- one auth round-trip already covers both reachability and
  auth-challenge.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:nsx-rest-connector-shim"`) and
delegates to `meho_backplane.operations.dispatch` with
`connector_id="nsx-rest-4.2"`. Pre-G0.6 chassis routes that still
invoke `Connector.execute` directly reach the dispatcher through
this shim; post-G0.6 callers (the `/api/v1/operations/call` route,
MCP `call_operation`, the CLI verbs once #615 lands) construct a
real `Operator` and call `dispatch` themselves.

### Shutdown

`aclose()` clears `self._session_tokens` and delegates to
`HttpConnector.aclose()` which closes every per-target httpx
client. No `DELETE /api/session/destroy` is issued -- NSX's session
has a documented idle timeout, and a per-target network call during
lifespan shutdown is more risk than benefit (a hung DELETE on an
unreachable target trips Kubernetes' 30-second
`terminationGracePeriod`). Revoke-on-close is a v0.2.next concern,
same posture vSphere takes for proactive refresh.

## Dependencies

- **httpx 0.28.x** -- per-target `AsyncClient` pool (inherited from
  `HttpConnector`); `client.post(url, data=<dict>)` for the
  form-encoded session create; automatic cookie-jar management for
  `JSESSIONID`.
- **tenacity 9.x** -- the inherited `@retry` decorator on
  `HttpConnector._request_json` retries connection errors and 5xx
  responses up to four attempts with exponential backoff; 4xx
  (including 401) propagates cleanly to the connector-level retry
  layer.
- **pydantic 2.13.x** -- `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models with `MappingProxyType` -wrapped
  `extras`; the connector constructs them by keyword.
- **respx 0.23.x (test-only)** -- the unit-test module mocks every
  request shape (form-encoded body, Set-Cookie response, X-XSRF-TOKEN
  response, 401 / 502 status sequences) without a network call.
- **structlog** -- a single `nsx_session_established` info event per
  successful session create; no other emit points in this skeleton.

## Known issues

- The connector cannot exercise any operation yet -- `endpoint_descriptor`
  rows for NSX REST land in #614 via G0.7 spec ingestion. The
  dispatch shim resolves `connector_id="nsx-rest-4.2"` but
  `op_id=<anything>` returns the dispatcher's "unknown operation"
  shape until the rows exist.
- Default session loader raises `NotImplementedError`. Production
  callers must inject `session_loader=...` on construction until
  G0.3 (#224) lands the operator-context Vault read path. Mirrors
  the `vmware_rest` precedent; both connectors pick up the live
  implementation in a single follow-up commit once G0.3 merges.
- Session-revoke on `aclose()` is deliberately omitted. NSX's idle
  timeout (~30 min for NSX Manager) reclaims abandoned sessions
  naturally; the cost of a hung DELETE during lifespan shutdown
  outweighs the audit-log neatness of an explicit revoke.

## References

- Issue: [G3.5-T1 #613](https://github.com/evoila/meho/issues/613).
- Parent Initiative: [G3.5 #368](https://github.com/evoila/meho/issues/368).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Sibling tasks: #614 (spec ingestion), #615 (CLI + MCP review + E2E).
- Precedent: `connectors/vmware_rest/connector.py` (session auth +
  fingerprint + probe + dispatch shim);
  `connectors/vmware_rest/__init__.py` (registration);
  `connectors/adapters/http.py` (`HttpConnector`);
  `connectors/base.py` (`Connector` ABC);
  `connectors/registry.py:108` (`register_connector_v2`).
- Consumer wrapper this contract mirrors:
  https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/nsx.sh
- NSX REST API guide:
  https://developer.broadcom.com/xapis/nsx-data-center-rest-api/latest/

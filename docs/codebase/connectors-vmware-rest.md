# Connector: vmware-rest (vSphere 8.5+ / 9.0)

## Overview

The `vmware-rest` connector is the hand-rolled `HttpConnector` subclass
that dispatches ingested vCenter REST operations under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple. It pairs with the G0.7 ingestion pipeline's auto-shim (which
makes ~1,275 `endpoint_descriptor` rows resolvable but not dispatchable)
to deliver real session-authenticated calls against vSphere 8.5+ /
ESXi 8.5+ targets.

Source: `backend/src/meho_backplane/connectors/vmware_rest/`.

## Key types

- **`VmwareRestConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vmware"`, `version="9.0"`,
  `impl_id="vmware-rest"`, `supported_version_range=">=8.5,<10.0"`,
  `priority=1`.
- **`VsphereTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the model satisfies the
  Protocol structurally without code edits here.
- **`VsphereSessionLoader`** (`session.py`) — async callable type
  resolving a target to `{"username": ..., "password": ...}`.
  Injectable on connector construction (`VmwareRestConnector(session_loader=…)`)
  so unit tests, integration tests, and pre-G0.3 production deploys
  override the default Vault loader.
- **`load_session_credentials_from_vault`** (`session.py`) — default
  loader, stubbed `NotImplementedError` until G0.3 lands the
  operator-context Vault read path. Mirrors the
  `load_kubeconfig_from_vault` pattern in `connectors/kubernetes/`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage.
2. Importing `meho_backplane.connectors.vmware_rest` triggers the
   module-level `register_connector_v2(product="vmware", version="9.0",
   impl_id="vmware-rest", cls=VmwareRestConnector)` call.
3. The registry's v2 table now resolves `(vmware, 9.0, vmware-rest)`
   to `VmwareRestConnector`. The G0.7 auto-shim's idempotency check
   (in `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.

### Per-target session

1. First call to `auth_headers(target, raw_jwt)` against a target whose
   name isn't in `self._session_tokens`:
   a. Acquires `self._session_lock` (asyncio.Lock).
   b. Calls `self._session_loader(target)` for the service-account
      credentials.
   c. POSTs `/api/session` with HTTP basic auth (creds["username"],
      creds["password"]).
   d. Parses the response body: a JSON-quoted string (vSphere 7.0+
      modern shape) or `{"value": "<token>"}` (pre-7.0 legacy shape;
      kept for vcsim cross-version compatibility).
   e. Caches the token under `target.name`.
2. Subsequent calls take the fast path: lock acquisition + cache hit +
   return.
3. The dispatcher's call path
   (`HttpConnector._request_json` / `_post_json`) reads
   `auth_headers()`, gets `{"vmware-api-session-id": "<token>"}`, sends
   it on every dispatched op against this target.

### fingerprint() / probe()

`fingerprint(target)` issues `GET /api/about` (auth headers injected
lazily via the cached session token); the response payload populates
the canonical `FingerprintResult`:

- `vendor="vmware"`
- `product` — via `product_from_line_id(payload.product_line_id)`
  (`vpx` → `vcenter`; `embeddedEsx`/`esx` → `esxi`; fall-through for
  unknown values; `""`/`None` → `"unknown"`)
- `version`, `build`, `edition` — straight from the payload
- `extras` — `uuid`, `full_name`, `product_line_id`, `api_type`,
  `os_type`

`probe(target)` delegates to `fingerprint()` and folds the boolean
reachable flag into a `ProbeResult`. Failure modes (TCP `ConnectError`,
TLS error, 401 from `/api/session`, 5xx from `/api/about`) surface as
`reachable=False` with the exception class + message in
`extras["error"]` / `ProbeResult.reason`.

### aclose()

1. Snapshot the cached session tokens, clear the dict.
2. For each `(target_name, token)` pair: issue `DELETE /api/session`
   with the `vmware-api-session-id` header. A failure (5xx, transport
   error, 401 from an expired session) is logged via structlog
   `vsphere_session_revoke_failed` / `vsphere_session_revoke_non_2xx`
   but doesn't block shutdown — Kubernetes' 30 s
   `terminationGracePeriod` would otherwise be at risk.
3. Delegate to `super().aclose()` to close the per-target httpx
   clients.

### execute()

Legacy shim — synthesises a system-tenant `Operator` and calls
`meho_backplane.operations.dispatch(...)` against the
`connector_id="vmware-rest-9.0"` encoding. Post-G0.6 callers
(`/api/v1/operations/call`, MCP `call_operation`, CLI verbs from #511)
construct a real `Operator` and call `dispatch()` directly; they don't
reach this method.

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` (G0.2-T3
  #242) — transport plumbing (retry, timeout, per-target pool,
  `_request_json` / `_post_json`).
- `meho_backplane.connectors.registry.register_connector_v2` (G0.6-T2
  #393).
- `meho_backplane.connectors.schemas` — `AuthModel`,
  `FingerprintResult`, `OperationResult`, `ProbeResult`.
- `meho_backplane.operations.dispatch` (G0.6-T5 #396) — invoked by
  `execute()`'s legacy shim.
- `httpx` (transitively via `HttpConnector`).
- `structlog` for structured log events.
- Test-only: `respx` for HTTP mocking in unit tests, `testcontainers`
  for the vcsim-backed integration test.

## Known issues / gaps

- **Default loader stubbed** — `load_session_credentials_from_vault`
  raises `NotImplementedError` until G0.3 (#224) lands. Production
  deploys must inject a custom `session_loader` at connector
  construction. Same pattern as `KubernetesConnector(kubeconfig_loader=…)`.
- **`auth_model` enum gating** — only `shared_service_account` (and
  `None` for pre-G0.3 targets) is accepted. `per_user` and
  `impersonation` raise `NotImplementedError`; both are deferred to
  v0.2.next.
- **No proactive 401 retry** — vSphere's ~5-minute idle timeout means
  a long-idle connection may see a 401 on the next dispatch. The
  caller-side retry logic in `_request_json` does not retry 401 by
  policy; an explicit refresh loop is v0.2.next polish.
- **`vi-json.yaml` ingestion not yet exercised** — T2 (#501) ships the
  parser extension for `$ref: #/components/parameters/*`. Once T3
  (#503) lands, the same connector dispatches the ~2,195 vi-json ops
  (vi-json shares the `vmware-api-session-id` header per
  `docs/vcenter-9.0/MANIFEST.md`).
- **Composites under separate Tasks** — T5 (#508) ships the 5 read
  composites; T6 (#509) ships the 8 write composites. Both depend on
  T4 (#504) for the `register_composite_operation()` helper.

## References

- Parent Initiative: [#227 G3.1 vmware-rest-9.0](https://github.com/evoila/meho/issues/227)
- Parent Task: [#498 G3.1-T1 VmwareRestConnector](https://github.com/evoila/meho/issues/498)
- G0.7 canary that ingested the rows this connector dispatches:
  [#408 G0.7-T8 vSphere canary](https://github.com/evoila/meho/issues/408)
  (closed via PR #493 on 2026-05-15).
- vSphere REST session contract:
  [vSphere Automation API security schema](https://developer.broadcom.com/xapis/vsphere-automation-api/latest/api-security-schema/).
- vcsim simulator: <https://github.com/vmware/govmomi/tree/main/vcsim>.
- Closest in-repo precedents:
  - Package layout + v2 registration pattern:
    `backend/src/meho_backplane/connectors/vault/__init__.py`.
  - Injectable-loader Protocol pattern:
    `backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py`.
  - `auth_headers` + `_request_json` HTTP plumbing:
    `backend/src/meho_backplane/connectors/adapters/http.py`.

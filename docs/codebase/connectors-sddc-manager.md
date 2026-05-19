# Connector: sddc-manager (SDDC Manager 9.0)

## Overview

The `sddc-manager` connector is the hand-rolled `HttpConnector` subclass that
dispatches SDDC Manager REST operations under the
`(product="sddc-manager", version="9.0", impl_id="sddc-rest")` registry triple.
G3.5-T4 (#616) shipped the skeleton — HTTP Basic auth, fingerprint, probe, and
the G0.6 dispatch shim. G3.5-T5 (#617) adds spec ingestion + operator-review
curation + ~8 read-only core ops. CLI verbs + MCP review + recorded-fixture E2E
arrive in G3.5-T6 (#618).

Source: `backend/src/meho_backplane/connectors/sddc_manager/`.

## Key types

- **`SddcManagerConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="sddc-manager"`, `version="9.0"`,
  `impl_id="sddc-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim (priority=0) defensively if both somehow register for the same
  triple.
- **`SddcTargetLike`** (`session.py`) — runtime-checkable Protocol capturing
  the minimum target shape the connector reads: `name`, `host`, `port`,
  `secret_ref`, `auth_model`, and `sso_realm`. `sso_realm` defaults to
  `"vsphere.local"` per the consumer wrapper contract; operators managing a
  custom SSO domain override it at the target level. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the model satisfies the Protocol
  structurally without code edits here.
- **`SddcCredentialsLoader`** (`session.py`) — async callable type resolving
  a target to `{"username": ..., "password": ...}`. Injectable on connector
  construction (`SddcManagerConnector(credentials_loader=...)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the default
  Vault loader.
- **`load_credentials_from_vault`** (`session.py`) — default loader, stubbed
  `NotImplementedError` until G0.3 lands the operator-context Vault read path.
  Mirrors the `load_session_credentials_from_vault` / `load_credentials_from_vault`
  shape in `connectors/vmware_rest/` and `connectors/nsx/`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.sddc_manager` triggers the
   module-level
   `register_connector_v2(product="sddc-manager", version="9.0", impl_id="sddc-rest", cls=SddcManagerConnector)`
   call.
3. The registry's v2 table now resolves `("sddc-manager", "9.0", "sddc-rest")`
   to `SddcManagerConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`, once #408's pipeline lands in main)
   no-ops on subsequent ingests against the same triple.

### Per-target credentials + HTTP Basic auth

SDDC Manager auth diverges from the NSX/vSphere precedents: no session cookie
or XSRF token is established; HTTP Basic is sent on every request.

1. `SddcManagerConnector.auth_headers(target)` is called for the first time
   against `target`.
2. `_load_credentials(target)` acquires the per-instance `asyncio.Lock`,
   checks the `self._creds_cache` dict (keyed on `target.name`), misses,
   calls the injected `credentials_loader(target)` → resolves to
   `{"username": ..., "password": ...}`.
3. Credentials are validated for the `"username"` and `"password"` keys; a
   missing key raises `RuntimeError` naming the target and the missing key.
4. The credentials are cached under `target.name` for the lifetime of the
   connector instance.
5. `auth_headers()` computes the username as `f"{creds['username']}@{sso_realm}"`
   where `sso_realm = target.sso_realm or "vsphere.local"`, then returns
   `{"Authorization": f"Basic {base64(username:password)}"}`.
6. Subsequent calls reuse the cached credentials; the loader is never
   called again for the same target.

Because HTTP Basic credentials are stateless server-side (no session to expire
or revoke), no 401-driven re-login is implemented. A 401 from a downstream
call propagates directly to the caller — it signals wrong credentials, not an
expired session.

### Fingerprint + probe

- `fingerprint(target)` issues `GET /v1/sddc-managers` through
  `HttpConnector._get_json` (with tenacity's connection-error + 5xx retry
  decorator). On success: reads `elements[0]` from the pagination envelope
  and returns
  `FingerprintResult(vendor="vmware", product="sddc-manager", version=...,
  build=..., reachable=True, extras={"id", "fqdn", "management_domain",
  "management_domain_id"})`. On transport, HTTP-status, or
  credentials-load failure: returns `reachable=False` with
  `extras["error"] = "<ExcType>: <message>"`.
- `probe(target)` delegates to `fingerprint` — one authenticated request
  covers both reachability and auth-challenge, same posture the vSphere and
  NSX precedents use.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:sddc-rest-connector-shim"`) and delegates
to `meho_backplane.operations.dispatch` with `connector_id="sddc-rest-9.0"`.
Pre-G0.6 chassis routes reach the dispatcher through this shim; post-G0.6
callers (the `/api/v1/operations/call` route, MCP `call_operation`, the CLI
verbs once #618 lands) construct a real `Operator` and call `dispatch`
themselves.

### Shutdown

`aclose()` clears `self._creds_cache` (no server-side session to revoke) and
delegates to `HttpConnector.aclose()` which closes every per-target httpx
client.

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool (inherited from
  `HttpConnector`); `Authorization: Basic` header computed by the connector
  using `base64.b64encode`.
- **tenacity 9.x** — the inherited `@retry` decorator on
  `HttpConnector._request_json` retries connection errors and 5xx responses
  up to four attempts with exponential backoff; 4xx propagates cleanly to
  the fingerprint/probe layer.
- **pydantic 2.13.x** — `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models; the connector constructs them by
  keyword.
- **respx 0.23.x (test-only)** — the unit-test module mocks every request
  shape without a network call.
- **structlog** — a single `sddc_manager_credentials_loaded` info event per
  successful first-use credential load; no other emit points in this skeleton.

## Known issues

- Default credentials loader raises `NotImplementedError`. Production callers
  must inject `credentials_loader=...` on construction until G0.3 (#224)
  lands the operator-context Vault read path. Mirrors the `vmware_rest` and
  `nsx` precedents; both connectors pick up the live implementation in a
  single follow-up commit once G0.3 merges.
- Operations are not yet available. `execute(target, op_id, ...)` resolves to
  "unknown operation" at the dispatcher layer until #617 lands the spec
  ingestion + endpoint_descriptor rows.
- HTTP Basic auth for VCF 9.x: the consumer wrapper (`scripts/sddc-manager.sh`)
  uses HTTP Basic with `username@sso_realm` format, and this connector mirrors
  that shape. Broadcom deprecated Basic Auth in VCF 4.x in favour of Bearer
  tokens (POST /v1/tokens); if a future VCF 9.x deployment rejects Basic auth,
  an auth-scheme migration task should be filed under Initiative #368.

## References

- Issues: [G3.5-T4 #616](https://github.com/evoila/meho/issues/616)
  (skeleton); [G3.5-T5 #617](https://github.com/evoila/meho/issues/617)
  (spec ingestion + read ops); [G3.5-T6 #618](https://github.com/evoila/meho/issues/618)
  (CLI + MCP review + E2E + onboarding doc).
- Parent Initiative: [G3.5 #368](https://github.com/evoila/meho/issues/368).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Precedent: `connectors/nsx/connector.py` (session auth + fingerprint +
  probe + dispatch shim); `connectors/vmware_rest/connector.py` (session auth);
  `connectors/adapters/http.py` (`HttpConnector`);
  `connectors/registry.py:108` (`register_connector_v2`).
- VCF API reference: https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/

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

- **`SddcCoreGroup`** (`core_ops.py`) — frozen dataclass carrying `group_key`,
  `name`, and `when_to_use` for one operator-reviewed LLM-grouping output group.
  8 entries in `SDDC_CORE_GROUPS` span the 8 SDDC Manager path families.
- **`SddcCoreOp`** (`core_ops.py`) — frozen dataclass carrying `op_id`,
  `group_key`, and `llm_instructions` for one curated read op. 9 entries in
  `SDDC_CORE_OPS` (2 in the `sddc-domains` group; 1 in every other group).
- **`SDDC_PATH_RULES`** (`core_ops.py`) — ordered tuple of `(path_prefix,
  group_key)` pairs used by `classify_sddc_op` to assign a VCF API path to its
  curated group. First match wins; covers the 8 top-level resource families
  (`releases`, `sddc-managers`, `domains`, `clusters`, `hosts`, `network-pools`,
  `bundles`, `tasks`).
- **`SDDC_PRODUCT`** (`core_ops.py`) — `"sddc"`, the value
  `parse_connector_id("sddc-rest-9.0")` extracts (first hyphen-segment of
  `impl_id="sddc-rest"`). This is the `product` stored in `endpoint_descriptor`
  and `operation_group` rows — **distinct** from `SddcManagerConnector.product`
  (`"sddc-manager"`), which is the v2 registry key and resolver target.
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

## `core_ops.py` — curation module

`core_ops.py` is the operator-review metadata store for the curated 9-op read
core. It mirrors the pattern `connectors/nsx/core_ops.py` established for NSX.

### `classify_sddc_op(op_id) -> str`

Strips the `METHOD:` prefix and walks `SDDC_PATH_RULES` in order, returning
the first matching `group_key` or `"none"` for uncurated paths. Used during
operator review to assign new ingested ops to groups without manual
classification.

### `apply_sddc_core_curation(review_service, *, tenant_id)`

The operator-review-time substrate call. After this call:

- All 8 curated groups land `review_status='enabled'`.
- Exactly the 9 ops in `SDDC_CORE_OPS` are `is_enabled=True`.
- Every other op in a curated group carries an operator-override audit row
  (`is_enabled=False`) that the `enable_group` cascade respects.
- Each curated op carries the reviewed `llm_instructions` blob.

The helper uses `ReviewService.get_review_payload` + `edit_op(is_enabled=False)`
(for non-core ops) + `edit_group` + `enable_group` + `edit_op(llm_instructions=...)`
in that order, exactly matching the `apply_nsx_core_curation` pattern.

Re-running is safe: `enable_group` short-circuits on already-enabled groups
(no audit row), but `edit_group` and `edit_op` always emit one audit row per
call even on no-op values. Intended posture is a one-shot curation after ingest.

### The 9 curated ops

| op_id | group | cli alias |
|---|---|---|
| `GET:/v1/releases/system` | `sddc-releases` | `sddc.about` |
| `GET:/v1/sddc-managers` | `sddc-managers` | `sddc.manager.list` |
| `GET:/v1/domains` | `sddc-domains` | `sddc.domain.list` |
| `GET:/v1/domains/{id}` | `sddc-domains` | `sddc.domain.info` |
| `GET:/v1/clusters` | `sddc-clusters` | `sddc.cluster.list` |
| `GET:/v1/hosts` | `sddc-hosts` | `sddc.host.list` |
| `GET:/v1/network-pools` | `sddc-network-pools` | `sddc.network_pool.list` |
| `GET:/v1/bundles` | `sddc-bundles` | `sddc.bundle.list` |
| `GET:/v1/tasks` | `sddc-tasks` | `sddc.workflow.list` |

Lifecycle-write ops (`workflow start`, `domain create`, `cluster expand`,
`host commission`) remain `staged` (never enabled) per Initiative #368 v0.2
scope.

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

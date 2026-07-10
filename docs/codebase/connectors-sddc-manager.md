# Connector: sddc-manager (SDDC Manager 9.0)

## Overview

The `sddc-manager` connector is the hand-rolled `HttpConnector` subclass that
dispatches SDDC Manager REST operations under the
`(product="sddc-manager", version="9.0", impl_id="sddc-rest")` registry triple.
G3.5-T4 (#616) shipped the skeleton — fingerprint, probe, and the G0.6 dispatch
shim. G3.5-T5 (#617) added spec ingestion + ingested read enablement. #2290
rebuilt the auth on the profile-derived `session_login_token` token session
(`POST /v1/tokens` → Bearer; the appliance rejects HTTP Basic). G3.5-T6 (#618)
shipped the CLI verb tree (`meho sddc-manager …`) + recorded-fixture E2E test.

#2306 converted the audited 12-read lab-audit set to first-class **typed** ops
(`source_kind="typed"`, `typed_ops.py` / `typed_reads.py`) that dispatch on a
fresh boot with zero catalog ingest. This is now the documented operational
surface; the wider ingested VCF catalog stays as profiled-dispatch breadth
(#2271) under its own `METHOD:path` op_ids (two surfaces, no resolver
shadowing — typed ops never resolve through `endpoint_descriptor` rows, #2262).
The four non-audited reads (release, domain detail, network-pools, bundles) and
the wider VCF catalog stay as ordinary `source_kind="ingested"` breadth, enabled
generically through `ReviewService.enable_reads`. The hand-curated
ingested-enable apparatus (`core_ops.py`) was retired in #2358 (T7 of #2266).

Source: `backend/src/meho_backplane/connectors/sddc_manager/`.

## Key types

- **`SDDC_PRODUCT`** (`__init__.py`) — `"sddc"`, the value
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
callers (the `/api/v1/operations/call` route, MCP `call_operation`, and the
`meho sddc-manager …` CLI verbs added in #618) construct a real `Operator` and
call `dispatch` themselves.

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

## `typed_ops.py` / `typed_reads.py` — typed read surface (#2306)

The audited 12-read lab-audit set ships as typed ops. `typed_reads.py` holds
the async handler bodies (each issues one `connector._get_json(...)` on the
token session); `connector.py` exposes them as thin bound-method shims
(`domain_list`, `credential_list`, …) so the dispatcher's `import_handler`
walk recovers each callable from its persisted `module.ClassName.method`
`handler_ref`. `typed_ops.py` carries the `SddcTypedOp` metadata tuple
(`SDDC_TYPED_OPS`) and the `register_sddc_typed_operations` registrar, queued
onto the lifespan runner in `__init__.py`. A raw `401` propagates to the
dispatcher's #2067 recovery arm, which calls the connector's public
`invalidate_session` hook (from #2290) and re-dispatches once.

| op_id | group | path |
|---|---|---|
| `sddc.domain.list` | `sddc-inventory` | `GET /v1/domains` |
| `sddc.domain.status` | `sddc-inventory` | `GET /v1/domains/{id}/status` |
| `sddc.cluster.list` | `sddc-inventory` | `GET /v1/clusters` |
| `sddc.host.list` | `sddc-inventory` | `GET /v1/hosts` |
| `sddc.vcenter.list` | `sddc-inventory` | `GET /v1/vcenters` |
| `sddc.nsxt_cluster.list` | `sddc-inventory` | `GET /v1/nsxt-clusters` |
| `sddc.credential.list` | `sddc-credentials` | `GET /v1/credentials` |
| `sddc.task.list` | `sddc-tasks-typed` | `GET /v1/tasks` |
| `sddc.system.info` | `sddc-platform` | `GET /v1/system` |
| `sddc.vcf_service.list` | `sddc-platform` | `GET /v1/vcf-services` |
| `sddc.manager.list` | `sddc-platform` | `GET /v1/sddc-managers` |
| `sddc.license.list` | `sddc-platform` | `GET /v1/license-keys` |

**Credential-read gating (`sddc.credential.list`).** SDDC Manager is the system
of record for nested-infra credentials; its `GET /v1/credentials` read returns
live account secrets. The typed op is gated via the existing mechanism:
`requires_approval=True` (so `policy_gate` routes it to the approval queue —
not dispatchable without operator approval), the op-id is on
`broadcast.events._CREDENTIAL_READ_OPS` (so `classify_op` returns
`credential_read` and audit/broadcast rows collapse to aggregate-only), and the
handler scrubs every secret-keyed value at the connector boundary
(`_redact_secrets`). Three layers; no secret ever rides the result. `safety_level`
is `caution`; the other 11 reads are `safe` / no-approval.

## Ingested breadth + read enablement

The audited operational reads (domains list + status, clusters, hosts, vcenters,
nsxt-clusters, credentials, tasks, system, vcf-services, sddc-managers,
license-keys) are typed ops (see the typed read surface above) and dispatch on a
fresh boot with zero catalog state.

The four non-audited reads (`GET:/v1/releases/system`, `GET:/v1/domains/{id}`,
`GET:/v1/network-pools`, `GET:/v1/bundles`) and the wider VCF API catalog land as
ordinary `source_kind="ingested"` `endpoint_descriptor` rows via G0.7 spec
ingestion and stay browsable as profiled-dispatch breadth. They are enabled
through the **generic review flow** — `ReviewService.enable_reads(connector_id,
tenant_id=...)` (REST `POST /api/v1/connectors/{connector_id}/enable-reads`, MCP
`meho.connector.enable_reads`).

The hand-curated ingested-enable apparatus (the `core_ops.py` module with its
`SDDC_CORE_OPS` / `SDDC_CORE_GROUPS` / `SDDC_PATH_RULES` constants and the
`classify_sddc_op` / `apply_sddc_core_curation` helpers) was retired in #2358
(T7 of #2266); read enablement is now generic, with no per-product curation code.

Lifecycle-write ops (`workflow start`, `domain create`, `cluster expand`,
`host commission`) remain `staged` (never enabled) per Initiative #368 v0.2
scope.

## CLI verbs (`cli/internal/cmd/sddc-manager/`)

The `meho sddc-manager` verb tree (added in #618) is a thin Cobra-over-HTTP
layer that POSTs to `/api/v1/operations/call` with `connector_id="sddc-rest-9.0"`
pre-baked. It is operator ergonomics, not a separate data path; the MCP
surface is unchanged (CLAUDE.md postulate 5).

Go package: `sddcmanager` (`cli/internal/cmd/sddc-manager/`).
Root entry point: `sddcmanager.NewRootCmd()` (called from `cli/internal/cmd/root.go`).

### Response envelope

SDDC Manager paginates list responses under an `elements[]` key
(`{"elements": [...], "pageMetadata": {...}}`). This differs from NSX's
`{"results": [...]}` envelope. The `decodeElementsResult` helper in
`sddc_manager.go` unwraps `elements[]` or falls back to a bare JSON array,
so all list-verb printers share the same decode path.

### Verb tree

| File | Command | `op_id` dispatched |
|---|---|---|
| `about.go` | `meho sddc-manager about` | `GET:/v1/releases/system` |
| `manager.go` | `meho sddc-manager manager list` | `GET:/v1/sddc-managers` |
| `domain.go` | `meho sddc-manager domain list` | `GET:/v1/domains` |
| `domain.go` | `meho sddc-manager domain info <id>` | `GET:/v1/domains/{id}` |
| `cluster.go` | `meho sddc-manager cluster list [--domain <id>]` | `GET:/v1/clusters` |
| `host.go` | `meho sddc-manager host list [--domain <id>] [--cluster <id>]` | `GET:/v1/hosts` |
| `network_pool.go` | `meho sddc-manager network-pool list` | `GET:/v1/network-pools` |
| `bundle.go` | `meho sddc-manager bundle list` | `GET:/v1/bundles` |
| `workflow.go` | `meho sddc-manager workflow list [--status <state>]` | `GET:/v1/tasks` |
| `operation.go` | `meho sddc-manager operation search <query>` | `GET /api/v1/operations/search` |
| `operation.go` | `meho sddc-manager operation call <op_id>` | `POST /api/v1/operations/call` |

All verbs share `--target`, `--json`, and `--backplane` flags. The
`--backplane` flag defaults to the URL written by the most recent `meho login`.

### `domain info` path parameter

`GET:/v1/domains/{id}` requires `{"id": "<domain-id>"}` in the params map for
the dispatcher's path substitution. The CLI verb passes this as
`params = map[string]any{"id": domainID}` before calling `dispatchOp`.

### Tests

`cli/internal/cmd/sddc-manager/sddc_manager_test.go` covers:
- `ConnectorID` constant pinned to `"sddc-rest-9.0"`.
- All 9 top-level verbs assembled by `NewRootCmd`.
- `decodeElementsResult` with `elements[]`-wrapped and bare-array payloads.
- Wire-level dispatch: `connector_id` baked, empty target → null, domain info
  sends id param, workflow list sends status filter, operation search sends
  connector_id.
- All verb printer renderers (table output, JSON path).

## Known issues

- Default credentials loader raises `NotImplementedError`. Production callers
  must inject `credentials_loader=...` on construction until G0.3 (#224)
  lands the operator-context Vault read path. Mirrors the `vmware_rest` and
  `nsx` precedents; both connectors pick up the live implementation in a
  single follow-up commit once G0.3 merges.
- HTTP Basic auth for VCF 9.x: the consumer wrapper (`scripts/sddc-manager.sh`)
  uses HTTP Basic with `username@sso_realm` format, and this connector mirrors
  that shape. Broadcom deprecated Basic Auth in VCF 4.x in favour of Bearer
  tokens (POST /v1/tokens); if a future VCF 9.x deployment rejects Basic auth,
  an auth-scheme migration task should be filed under Initiative #368.

## References

- Issues: [G3.5-T4 #616](https://github.com/evoila/meho/issues/616)
  (skeleton); [G3.5-T5 #617](https://github.com/evoila/meho/issues/617)
  (spec ingestion + read ops); [G3.5-T6 #618](https://github.com/evoila/meho/issues/618)
  (CLI verbs + E2E + this doc update).
- Parent Initiative: [G3.5 #368](https://github.com/evoila/meho/issues/368).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Operator onboarding: [`docs/cross-repo/sddc-manager-onboarding.md`](../cross-repo/sddc-manager-onboarding.md) — wrapper-flip recipe.
- CLI source: [`cli/internal/cmd/sddc-manager/`](../../cli/internal/cmd/sddc-manager/).
- E2E integration test: [`backend/tests/test_connectors_sddc_manager_e2e.py`](../../backend/tests/test_connectors_sddc_manager_e2e.py).
- Precedent: `connectors/nsx/connector.py` (session auth + fingerprint +
  probe + dispatch shim); `connectors/vmware_rest/connector.py` (session auth);
  `connectors/adapters/http.py` (`HttpConnector`);
  `connectors/registry.py:108` (`register_connector_v2`).
- VCF API reference: https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/

# Connector: vcf-operations (VCF Operations / vROps 9.0)

## Overview

The `vcf-operations` connector is the hand-rolled `HttpConnector` subclass
that dispatches VMware Aria Operations (formerly vRealize Operations Manager;
"vROps") REST operations under the
`(product="vrops", version="9.0", impl_id="vrops-rest")` registry triple.
G3.6-T1 (#829) shipped the skeleton; #2395 rebuilt its auth on an
acquired-token session (`OpsToken`) after live VCF Operations 9.0.2 rejected
the original stateless HTTP Basic. The audited read set ships as typed ops
(#2303); the remaining `/suite-api` breadth is ingested/curated.

Source: `backend/src/meho_backplane/connectors/vcf_operations/`.

The connector is **single-plane** (one API surface at `/suite-api/api/*`)
and **session-stateful**: it acquires a token via
`POST /suite-api/api/auth/token/acquire` and presents it as
`Authorization: OpsToken <token>` on every request, re-acquiring on an
idle-expired session through the dispatcher's #2067 seam. This mirrors the
vRLI (#830) token-session shape; Fleet #831 adds a product-specific
fingerprint path, Automation #832 adds dual-plane auth + vhost routing.

## Key types

- **`VcfOperationsConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vrops"`, `version="9.0"`,
  `impl_id="vrops-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
  Holds a per-target session-token cache (`_session_tokens`, keyed on the
  tenant-unique `(tenant_id, id)` tuple) plus the shared `CredentialsCache`.
- **`VcfOperationsTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: the shared
  `name` / `host` / `port` / `secret_ref` / `auth_model` quintet plus one
  vROps-specific field, `auth_source` (optional identity-domain label).
  Replaced by the concrete `Target` model once `auth_source` lands as a
  column in `meho_backplane.targets` (G0.3 #224); the model satisfies the
  Protocol structurally without code edits here.
- **`VcfOperationsCredentialsLoader`** (`session.py`) — type alias for the
  shared `VcfCredentialsLoader` callable that resolves a target to
  `{"username": ..., "password": ...}`. Injectable on connector construction
  (`VcfOperationsConnector(credentials_loader=...)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the default
  Vault loader.
- **`load_credentials_from_vault`** (re-exported from
  `connectors/_shared/vcf_auth.py`) — default loader; the live
  operator-context per-target Vault KV-v2 read (G3.10-T2 #946). The same
  loader serves vROps / vRLI / Fleet (#841 lifted the helper into `_shared`).
- **`SessionLoginError`** (re-exported from `connectors/_shared/vcf_auth.py`)
  — raised when `token/acquire` returns a non-2xx or a token-less 2xx; its
  `ConnectorAuthError` subclass carries the 401/403 establish-auth cause the
  dispatcher maps to `connector_auth_failed`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_operations` triggers the
   module-level
   `register_connector_v2(product="vrops", version="9.0", impl_id="vrops-rest", cls=VcfOperationsConnector)`
   call.
3. The registry's v2 table now resolves `("vrops", "9.0", "vrops-rest")` to
   `VcfOperationsConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`) no-ops on subsequent ingests against
   the same triple.

The v1 `register_connector` entry point is deliberately **not** called —
the v1 entry would land as `("vrops", "", "")` and confuse
`resolve_connector`'s tie-break ladder. Same pattern Harbor / SDDC Manager
/ NSX / VCF Automation established. (A separate wildcard-fallback
`("vrops", "", "")` v2 entry is registered so an unfingerprinted target
still resolves — the versioned entry wins the tie-break when both exist.)

### Authentication (acquired-token session, `OpsToken`)

Live VCF Operations 9.0.2 rejects stateless HTTP Basic on
`/suite-api/api/*` (the earlier skeleton's design assumption was false —
#2395). The connector establishes a session token and presents it on every
request. The flow:

1. `auth_headers(target, operator)` checks `target.auth_model` via the
   shared `is_acceptable_auth_model` predicate. Anything other than
   `shared_service_account` / the enum member / `None` (the pre-G0.3
   sentinel) raises `NotImplementedError` naming both the target and the
   requested mode.
2. `_session_token(target, operator)` returns the cached token on a hit.
   On first use (under a per-connector lock, keyed on the tenant-unique
   `(tenant_id, id)` tuple) it loads credentials via the injected
   `VcfOperationsCredentialsLoader` (cached in the shared `CredentialsCache`;
   missing `"username"`/`"password"` surfaces as `RuntimeError` naming the
   target) and POSTs them to `POST /suite-api/api/auth/token/acquire`
   through the shared `vcf_session_login` helper. The 200 body
   `{"token", "validity", "expiresAt", "roles"}` yields `token`, which is
   cached.
3. Returns `{"Authorization": "OpsToken <token>"}`. The 9.x-native
   `OpsToken` scheme is preferred over the legacy `vRealizeOpsToken` alias
   (the connector pins `>=9.0,<10.0`); neither `Basic` nor `Bearer` is
   accepted by the appliance.

The full `operator` is threaded through so the live default loader reads
the per-target Vault secret under the operator's identity. An empty
`operator.raw_jwt` fails closed (`VaultCredentialsReadError`) before the
cache lookup, so a token primed by an authenticated caller can't leak to a
system-initiated caller.

Both dispatch paths — the typed reads and the generic-ingested path — attach
auth through the same `auth_headers` seam (`adapters/http.py` at the
`_request_json` and `_post_json` request sites), so both carry `OpsToken`.

### Optional `authSource` federation

vROps can federate identity through multiple sources (the local realm,
`vIDM`, an Active Directory realm name, etc.). When `target.auth_source`
is set, it rides the **`token/acquire` body** as `"authSource"` — its
token-era home. When unset (`None` / `""`), the field is omitted and vROps
authenticates against its default local realm. The accepted values are
operator-configured per vROps deployment; the connector passes the string
through verbatim. (The pre-token skeleton rode this as a `?auth-source=`
query parameter on every request; that mechanism is deleted — the appliance
reads `authSource` only at acquire time.)

### Session-expiry recovery (the #2067 seam)

The connector advertises a duck-typed `invalidate_session(target)` hook. On
an auth-class status (401) from a dispatched op, the generic-ingested
dispatch path evicts the cached token via this hook and re-dispatches the op
exactly once (G0.29-T2 #2067) — so an idle-expired session re-acquires there
rather than failing until a process restart. A second auth failure (the
re-acquire also failed) falls through to `connector_auth_failed`. A 401/403
at `token/acquire` itself surfaces as `connector_auth_failed` with
`cause=session_establish_401` / `_403` via the shared `ConnectorAuthError`.

The typed connector carries no `ExecutionProfile`, so the dispatcher
classifies its 401 against the typed-connector global auth-failed set
(`{401, 440}`). The base `HttpConnector._request_json` tenacity decorator
(3 retries, exponential backoff, connection errors + 5xx only — never 4xx)
is inherited unchanged.

### Fingerprint

`GET /suite-api/api/versions/current` returns a JSON payload shaped
`{"releaseName": "...", "buildNumber": <int>, "humanlyReadableReleaseName"?: "..."}`.
The connector lifts:

- `releaseName` → `FingerprintResult.version`.
- `buildNumber` → `FingerprintResult.build` (stringified — the wire-level
  field is an integer; the result-model field is `str | None`).
- `humanlyReadableReleaseName` → `extras["humanly_readable_release_name"]`
  (some 9.0 builds emit it, some don't).

The version call rides the connector's `OpsToken` session like every other
read (through `_get_json`), so a credential / session-establish failure is
part of the fingerprint's failure surface.

On transport, session-establish, or status failure, the result carries
`reachable=False` and `extras["error"]` set to `f"{type(exc).__name__}: {exc}"`
— the same pattern Harbor / SDDC Manager / NSX use.

### Probe

`probe(target)` delegates to `fingerprint(target)`. vROps does not expose
a dedicated `/health` endpoint distinct from the version surface, so the
fingerprint call is the right reachability probe. `reachable=True` ⇒
`ok=True`; `reachable=False` ⇒ `ok=False` with `extras["error"]`
surfaced as the probe's `reason`. Harbor's purpose-built
`/api/v2.0/health` is the exception, not the rule.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:vcf-operations-connector-shim"`) and
delegates to `meho_backplane.operations.dispatch` with
`connector_id="vrops-rest-9.0"`. Pre-G0.6 chassis routes reach the
dispatcher through this shim; post-G0.6 callers (the
`/api/v1/operations/call` route, MCP `call_operation`, and the
`meho vcf-operations …` CLI verbs added in #837) construct a real
`Operator` and call `dispatch` themselves.

The connector exposes two operation surfaces: a **typed** read set
(below) that works with zero catalog ingest, plus the **ingested**
`/suite-api` breadth catalog seeded from `tests.acceptance._vrops_canary_fixtures`.

### Typed read operations (#2303, Initiative #2266 T3)

The adopter's *audited* vROps read set (audit #2294) ships as **typed**
ops (`source_kind="typed"`) in `typed_ops.py`, dispatched directly on the
connector's `OpsToken` session — no ingested descriptor row, so they work
on a fresh boot with zero catalog ingest (the #2262 no-shadow invariant).
Handlers are bound methods on
`VcfOperationsConnector`; a module-level `register_vcf_operations_typed_operations`
registrar is queued via `register_typed_op_registrar` in the package
`__init__` and run by the lifespan.

- **`vrops.liveness`** — `GET /suite-api/api/versions/current`. Appliance
  liveness + identity (release/build); the same surface `probe()` uses.
  The adopter named the probe `casa/health`, but the CaSA API is
  private/undocumented, so the documented version surface is the grounded
  liveness op. Supersedes the former curated `vrops.about`.
- **`vrops.alert.list`** — `GET /suite-api/api/alerts`. Alert triage,
  filtered by `activeOnly` / `alertCriticality` / `alertStatus` /
  `resourceId` with pagination. Supersedes the curated
  `GET:/suite-api/api/alerts` ingested row.
- **`vrops.resource.query`** — `POST /suite-api/api/resources/query`. A
  body-shaped POST carrying a typed `ResourceQuerySpec` subset (match on
  `resourceKind` / `name` / `regex` / `adapterKind` / state / status /
  health / parent / `statKey`), paginated via `page` / `pageSize` query
  params.

All three are `safety_level="safe"`, `requires_approval=False`,
read-only. Each rides the connector's `OpsToken` session; `authSource`
federation lives in the acquire body, not on the reads. `vrops.resource.query`
encodes its `page`/`pageSize` pagination onto the request path (the base
`_post_json` takes no `params` mapping).

The remaining 6 ingested-browse ops (resource list/get, alert definitions,
symptoms, recommendations, super metrics) stay browsable until Initiative
#2266 T7 retires the apparatus.

### Shutdown

`aclose()` clears the in-memory session-token cache and the shared
`CredentialsCache` under their locks (so a post-`aclose` reuse of the same
connector instance starts clean) and delegates to `HttpConnector.aclose()`
which closes every per-target httpx client. No server-side token revoke is
issued — the vROps token idle-expires on the appliance (same posture
NSX / vRLI take).

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool inherited from
  `HttpConnector`; the per-target client carries the base URL
  (`https://{host}` or `https://{host}:{port}` when port ≠ 443),
  `Timeout(connect=5, read=30, write=30, pool=5)`, and
  `follow_redirects=True`. The `token/acquire` POST goes through the shared
  `vcf_session_login` helper on this client (bypassing the tenacity
  decorator by design — one attempt, surface the failure cleanly).
- **tenacity 9.x** — base `HttpConnector._request_json` carries the retry
  decorator (3 retries / exponential backoff / connection errors + 5xx
  only); the connector inherits it unchanged (no `_request_json` override).
- **pydantic 2.13.x** — `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models; the connector constructs them by
  keyword.
- **structlog 25.x** — credential-load events log through the shared
  `CredentialsCache`; the connector itself doesn't add structlog calls
  beyond what the base + cache emit.
- **respx 0.23.x (test-only)** — the unit-test module mocks every request
  shape (`token/acquire` happy path + 401/missing-token/empty-token,
  `authSource` in the acquire body set + unset, OpsToken scheme regression
  on both the typed and ingested paths, the #2067 401 → re-acquire → retry
  recovery, fingerprint/probe reachable + unreachable) without a network
  call.

## Known issues

- **Credential-cache eviction on rotation** — a rotated service-account
  password is caught at `token/acquire` (401 → `connector_auth_failed`),
  but the cached *credential* is not auto-evicted; the family-wide
  credential-cache eviction hook is a sibling task (#2396). Until then a
  process restart (which clears both caches on `aclose`) is the workaround.
- **Ingested-curation retirement pending** — the 6 remaining
  ingested-browse ops still depend on per-deploy catalog state (an ingest of
  the vROps `/suite-api` spec + operator review); the typed reads above do
  not. Retiring the whole ingested-curation apparatus is Initiative #2266 T7.

## References

- **Task (skeleton)**: <https://github.com/evoila/meho/issues/829>
- **Task (OpsToken auth rebuild)**: <https://github.com/evoila/meho/issues/2395>
- **Parent initiative**: <https://github.com/evoila/meho/issues/369>
- **Parent goal**: <https://github.com/evoila/meho/issues/214>
- **Sibling skeletons (same Initiative wave)**:
  <https://github.com/evoila/meho/issues/830> (vRLI),
  <https://github.com/evoila/meho/issues/831> (Fleet),
  <https://github.com/evoila/meho/issues/832> (VCF Automation, merged).
- **Shared auth scaffolding**: `backend/src/meho_backplane/connectors/_shared/vcf_auth.py`
  ([#841](https://github.com/evoila/meho/issues/841)).
- **Co-area connector docs**: [`connectors-harbor.md`](connectors-harbor.md)
  (closest HTTP-Basic precedent), [`connectors-sddc-manager.md`](connectors-sddc-manager.md),
  [`connectors-vcf-automation.md`](connectors-vcf-automation.md),
  [`connectors-vcf-auth-shared.md`](connectors-vcf-auth-shared.md).
- **vROps Suite API**:
  <https://developer.broadcom.com/xapis/vcf-operations-api/latest/>
- **Acquire an authentication token (VCF Operations 9.0)**:
  <https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/understanding-the-vr-ops-api/getting-started-with-the-api/acquire-an-authentication-token.html>
- **Release-readiness rubric**:
  [`connector-release-readiness.md`](connector-release-readiness.md).

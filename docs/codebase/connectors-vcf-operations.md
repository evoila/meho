# Connector: vcf-operations (VCF Operations / vROps 9.0)

## Overview

The `vcf-operations` connector is the hand-rolled `HttpConnector` subclass
that dispatches VMware Aria Operations (formerly vRealize Operations Manager;
"vROps") REST operations under the
`(product="vcf-operations", version="9.0", impl_id="vrops-rest")` registry
triple. G3.6-T1 (#829) shipped the skeleton — HTTP Basic auth with an
optional `auth-source` query parameter, the `auth_model` boundary gate,
fingerprint, probe, and the G0.6 dispatch shim. G3.6-T2 (#833) will add
spec ingestion + operator-review curation against the vROps `/suite-api`
OpenAPI spec; G3.6-T3 (#837) will ship the `meho vcf-operations <op>` CLI
verb tree + recorded-fixture E2E.

Source: `backend/src/meho_backplane/connectors/vcf_operations/`.

The connector is **single-plane** (one API surface at `/suite-api/api/*`)
and **stateless** (Basic auth on every request, no session token). This
is the simplest of the four G3.6 management-plane connectors — vRLI #830
adds session-token + 401-retry-once, Fleet #831 adds product-specific
fingerprint path, Automation #832 adds dual-plane auth + vhost routing.

## Key types

- **`VcfOperationsConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vcf-operations"`, `version="9.0"`,
  `impl_id="vrops-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
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
  `connectors/_shared/vcf_auth.py`) — default loader, stubbed
  `NotImplementedError` until the live operator-context per-target Vault
  read lands. The same stub serves vROps / vRLI / Fleet (#841 lifted the
  helper into `_shared` to centralise the swap-over point).

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_operations` triggers the
   module-level
   `register_connector_v2(product="vcf-operations", version="9.0", impl_id="vrops-rest", cls=VcfOperationsConnector)`
   call.
3. The registry's v2 table now resolves `("vcf-operations", "9.0",
   "vrops-rest")` to `VcfOperationsConnector`. The G0.7 auto-shim's
   idempotency check (in `ensure_connector_class_registered`) no-ops on
   subsequent ingests against the same triple.

The v1 `register_connector` entry point is deliberately **not** called —
the v1 entry would land as `("vcf-operations", "", "")` and confuse
`resolve_connector`'s tie-break ladder. Same pattern Harbor / SDDC Manager
/ NSX / VCF Automation established.

### Authentication (HTTP Basic, stateless)

vROps' `/suite-api/api/*` surface accepts HTTP Basic on every request — no
session cookie or token is established. The flow:

1. First call against a target loads credentials via the injected
   `VcfOperationsCredentialsLoader` and caches them under `target.name` in
   the shared `CredentialsCache`. Missing-key (`"username"` /
   `"password"`) returns from the loader surface as `RuntimeError` naming
   both the target and the missing key.
2. `auth_headers(target, operator)` checks `target.auth_model` via the
   shared `is_acceptable_auth_model` predicate. Anything other than
   `shared_service_account` / the enum member / `None` (the pre-G0.3
   sentinel) raises `NotImplementedError` naming both the target and the
   requested mode.
3. Returns `{"Authorization": "Basic <b64>"}` computed from the cached
   credentials.

`raw_jwt` is accepted for ABC-signature compatibility but unused —
`SHARED_SERVICE_ACCOUNT` mode authenticates with a Vault-sourced service
account, not the operator's OIDC token.

### Optional `auth-source` query parameter

vROps can federate identity through multiple sources (the local realm,
`vIDM`, an Active Directory realm name, etc.). When `target.auth_source`
is set, the connector appends `?auth-source=<value>` as a query parameter
on every authenticated request through `_request_json`. When unset
(`None` / `""`), the query parameter is omitted and vROps falls back to
its local realm. The accepted values (the local realm label, an AD
realm name, etc.) are operator-configured per vROps deployment; the
connector passes the string through verbatim.

The `_request_json` override merges caller-supplied params with the
auth-source contribution; **caller-supplied params win on key conflict**.
The override sits on top of `HttpConnector._request_json`, preserving its
tenacity retry decorator (3 retries on idempotent verbs, exponential
backoff, only on connection errors + 5xx — not on 4xx, including 401).

### No 401-retry-once wrapper

vROps Basic auth is stateless. A 401 always means "bad credentials" (or a
misconfigured `auth-source`); retrying with the same credentials would
not help. The shared `CredentialsCache.invalidate(target)` is the right
seam for a future rotation-event admin endpoint to drop the cache between
the rotation and the next dispatch — but at the transport layer, no
retry loop is wired.

This contrasts with vRLI (#830) and Fleet (#831), which establish session
tokens via the shared `vcf_session_login` helper and add a 401-retry-once
wrapper in the consumer connector around downstream calls. vROps doesn't
need that — same reason Harbor doesn't.

### Fingerprint

`GET /suite-api/api/versions/current` returns a JSON payload shaped
`{"releaseName": "...", "buildNumber": <int>, "humanlyReadableReleaseName"?: "..."}`.
The connector lifts:

- `releaseName` → `FingerprintResult.version`.
- `buildNumber` → `FingerprintResult.build` (stringified — the wire-level
  field is an integer; the result-model field is `str | None`).
- `humanlyReadableReleaseName` → `extras["humanly_readable_release_name"]`
  (some 9.0 builds emit it, some don't).

The version endpoint is unauthenticated on vROps; the connector still sends
Basic auth on the call because (a) the appliance ignores unsolicited auth
headers on unauthenticated paths, (b) keeping a single `_request_json`
transport path simplifies auditing, and (c) the Harbor / SDDC Manager /
NSX precedents all do the same.

On transport or status failure, the result carries `reachable=False` and
`extras["error"]` set to `f"{type(exc).__name__}: {exc}"` — the same
pattern Harbor / SDDC Manager / NSX use.

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

Until G3.6-T2 (#833) ingests the vROps OpenAPI spec, no operations exist
in the `endpoint_descriptor` table for the `vrops-rest-9.0` connector_id
— every `execute(..., op_id, ...)` call resolves to "unknown operation"
at the dispatcher layer. This is the correct behaviour for a
registered-but-empty connector at this Task's stage.

### Shutdown

`aclose()` clears the shared `CredentialsCache` under its lock (so a
post-`aclose` reuse of the same connector instance starts clean) and
delegates to `HttpConnector.aclose()` which closes every per-target httpx
client.

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool inherited from
  `HttpConnector`; the per-target client carries the base URL
  (`https://{host}` or `https://{host}:{port}` when port ≠ 443),
  `Timeout(connect=5, read=30, write=30, pool=5)`, and
  `follow_redirects=True`. The `_request_json` override threads
  `auth-source` into the merged params; httpx merges params into the
  request URL.
- **tenacity 9.x** — base `HttpConnector._request_json` carries the
  retry decorator (3 retries / exponential backoff / connection errors +
  5xx only). The vROps override preserves the decorator by calling
  `super()._request_json(...)` with the merged params.
- **pydantic 2.13.x** — `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models; the connector constructs them by
  keyword.
- **structlog 25.x** — credential-load events log through the shared
  `CredentialsCache`; the connector itself doesn't add structlog calls
  beyond what the base + cache emit.
- **respx 0.23.x (test-only)** — the unit-test module mocks every request
  shape (auth header, auth-source query param both set + unset, missing
  credentials, fingerprint reachable + unreachable, probe ok / not-ok)
  without a network call. Recorded-fixture integration tests will land in
  G3.6-T3 (#837) under `backend/tests/fixtures/vcf/`.

## Known issues

- **Default loader stub** — `load_credentials_from_vault` raises
  `NotImplementedError` until the operator-context per-target Vault read
  lands (tracked under Goal #214). The supported workaround is to inject
  a custom `credentials_loader` on `VcfOperationsConnector` at
  construction time. Same stub serves vRLI + Fleet via the shared
  `_shared/vcf_auth.py`.
- **No 401-retry on credential rotation** — a credential rotation event
  on the operator side requires explicit cache invalidation via
  `CredentialsCache.invalidate(target)`. Until the admin endpoint for
  rotation lands, the workaround is to restart the backplane process
  (which clears the cache on `aclose`).
- **No operations yet** — the connector is registered but no
  `endpoint_descriptor` rows exist; dispatch against any `op_id`
  resolves to "unknown operation" until G3.6-T2 (#833) ingests the
  vROps `/suite-api` OpenAPI spec.

## References

- **Task**: <https://github.com/evoila/meho/issues/829>
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
  <https://developer.broadcom.com/xapis/vrealize-operations-manager-api/latest/>
- **Release-readiness rubric**:
  [`connector-release-readiness.md`](connector-release-readiness.md) — vROps
  starts at **State 0.5** (class registered, no ops yet).

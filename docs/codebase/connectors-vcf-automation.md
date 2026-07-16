# Connector: vcf-automation (VCF Automation 9.0, dual-plane)

## Overview

The `vcf-automation` connector is the hand-rolled `HttpConnector` subclass that
dispatches VCF Automation REST operations under the
`(product="vcfa", version="9.0", impl_id="vcfa-rest")` registry triple
(the `product` slug was unified to `"vcfa"` in #1814 — it matches what
`parse_connector_id("vcfa-rest-9.0")` derives for the descriptor rows).
G3.6-T10 (#832) shipped the skeleton — dual-plane auth (provider +
tenant), vhost / FQDN routing, fingerprint, probe, and the G0.6 dispatch shim.
G3.6-T11 (#836) added the dual-plane spec ingestion + operator-review
curation. G3.6-T12 (#840) shipped the recorded-fixture E2E. The operator
runbook lives at `docs/cross-repo/g36-vcfa-canary.md`.

**Typed reads (T5 #2305).** VCFA ships **no vendor OpenAPI spec** (the
provider plane publishes none; the tenant plane ships only Swagger 2.0
fragments the ingest parser rejects by decision #2090), so the curated
`core_ops` ingested-curation path is dispatch-inert on a real deploy —
there is nothing to ingest. The **audited read set** (evoila/meho#2294:
org/region list, provider health, `/iaas/api/projects` + tenant `about`)
is therefore served by `source_kind="typed"` ops (`typed_ops.py`) that
dispatch through the connector's own dual-plane session with **zero
catalog state**. Five ops:

| op_id | plane | path |
|---|---|---|
| `vcfa.provider.org.list` | provider | `GET /cloudapi/1.0.0/orgs` |
| `vcfa.provider.region.list` | provider | `GET /cloudapi/1.0.0/regions` |
| `vcfa.provider.health` | provider | `GET /cloudapi/1.0.0/site` |
| `vcfa.tenant.project.list` | tenant | `GET /iaas/api/projects` |
| `vcfa.tenant.about` | tenant | `GET /iaas/api/about` |

Each op **declares the plane it rides**; `typed_ops._validate_typed_op_planes`
asserts at import time that the declared `plane` matches
`plane_for_path(op.path)`, so a drift fails the import rather than
surfacing as a misrouted HTTP 401. The `org create` write
(`POST /cloudapi/1.0.0/orgs`) is deliberately out of scope — a first
write on a read-only connector belongs in a G3.x-mold approval-gated
write-surface initiative. The remaining `core_ops` /`_core_data`
ingested-curation surface is now the 5-group / 6-op browse remainder
(the two get-by-id ops, provider users list, tenant deployment/blueprint
browse) — declined from typed conversion because they are not in the
operator-run audited set.

`register_typed_operations` (a classmethod on the connector, queued onto
the lifespan registrar list via `register_vcfa_typed_operations` in
`__init__.py`) upserts the five descriptors on startup — the same
argocd / bind9 / Kubernetes typed-registrar shape.

Source: `backend/src/meho_backplane/connectors/vcf_automation/`.

The connector is **dual-plane**: a single registry triple covers both the
vCloud-Director-derived provider plane (paths under `/cloudapi/*` and the
classic `/api/*` family) and the Aria-IaaS-derived tenant plane (paths under
`/iaas/api/*`). Each plane has its own login flow, its own cached token, and
its own 401-driven re-login lock. The dual-source shape parallels vSphere's
`vcenter.yaml` + `vi-json.yaml` (`connectors/vmware_rest/`); the dual-auth
shape is unique to VCFA because the two API planes are independent identity
domains.

## Key types

- **`VcfAutomationConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vcfa"`, `version="9.0"`,
  `impl_id="vcfa-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
- **`VcfAutomationConfigurationError`** (`connector.py`) — `RuntimeError`
  subclass raised when a target's configuration prevents the connector from
  running (today: IP host with no `fqdn` set). The subclass lets the
  fingerprint/probe layer keep its existing `except (httpx.HTTPError, OSError,
  RuntimeError)` clause without a separate exception branch.
- **`VcfAutomationTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`, `host`,
  `port`, `secret_ref`, `auth_model`, plus four VCFA-specific fields: `fqdn`
  (load-bearing vhost override), `domain` (org / SSO realm forwarded on the
  tenant login body), `provider_username` (verbatim Basic-auth user for the
  provider plane, typically `admin@System`), `provider_secret_ref` (optional
  Vault path for a distinct provider-plane password). Replaced by the
  concrete `Target` model once those columns land in
  `meho_backplane.targets`; the model satisfies the Protocol structurally
  without code edits here.
- **`VcfAutomationCredentialsLoader`** (`session.py`) — async callable type
  resolving a target to `{"username": ..., "password": ...}`. Injectable on
  connector construction (`VcfAutomationConnector(credentials_loader=...)`)
  so unit tests, integration tests, and pre-G0.3 production deploys override
  the default Vault loader. The same loader is called twice when
  `target.provider_secret_ref` differs from `target.secret_ref` — once per
  plane — so a single read path serves both planes.
- **`load_credentials_from_vault`** (`session.py`) — default loader, stubbed
  `NotImplementedError` until the live operator-context per-target Vault read
  lands. Mirrors `load_credentials_from_vault` in `connectors/sddc_manager/`
  and `load_session_credentials_from_vault` in `connectors/nsx/` /
  `connectors/vmware_rest/`.
- **`VcfaCoreGroup` / `VcfaCoreOp`** (`core_ops.py`) — frozen dataclasses
  carrying the operator-review metadata for one curated group / op. Each
  entry includes a `plane` field (`"provider"` or `"tenant"`) that is
  asserted at module import time to match
  `_routing.plane_for_path(path)` — a path-vs-plane drift fails import
  rather than surfacing as a misrouted 401 in production.
- **`VCFA_TYPED_OPS` / `VcfaTypedOp` / `VCFA_TYPED_WHEN_TO_USE_BY_GROUP`**
  (`typed_ops.py`) — the five typed read ops (T5 #2305) and their two
  per-plane groups (`vcfa-provider-reads`, `vcfa-tenant-reads`). Each
  `VcfaTypedOp` carries a `plane` + `path`; the module's
  `_validate_typed_op_planes()` cross-checks them at import.
- **`VCFA_CORE_GROUPS` / `VCFA_CORE_OPS`** (`core_ops.py`) — the
  ingested-curation browse remainder: 5 curated groups (3 provider + 2
  tenant) and 6 curated ops (3 provider + 3 tenant) left after the
  audited read set moved to `typed_ops.py`. Every group's `when_to_use`
  names its plane explicitly so the agent's `list_operation_groups`
  step routes correctly across the dual-plane surface.
- **`VCFA_PRODUCT` / `VCFA_VERSION` / `VCFA_IMPL_ID` /
  `VCFA_CONNECTOR_ID`** (`core_ops.py`) — DB-side keys. Since #1814 the
  registry key `VcfAutomationConnector.product` was unified to `"vcfa"`,
  matching `VCFA_PRODUCT` (what `parse_connector_id("vcfa-rest-9.0")`
  extracts). All `endpoint_descriptor` and `operation_group` rows carry
  `product="vcfa"`.
- **`apply_vcfa_core_curation`** (`core_ops.py`) — async helper
  driving `ReviewService.edit_group` + `enable_group` +
  `edit_op(is_enabled=False)` (for non-core ops in curated groups)
  + `edit_op(llm_instructions=…)` (for the 6 core ops) so exactly
  the curated set is dispatchable after the call returns. Mirrors
  `apply_nsx_core_curation` / `apply_harbor_core_curation`
  verbatim; the audit-log-driven operator-override exclusion is the
  mechanism that threads "enable only ops X, Y, Z under group G"
  through `ReviewService.enable_group`'s cascade.
- **`classify_vcfa_op` / `VCFA_PATH_RULES`** (`core_ops.py`) —
  path-prefix classifier. Tenant rules (`/iaas/api/*`) are listed
  first defensively; the two planes never share a path family so
  ordering is not load-bearing today.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_automation` triggers the
   module-level
   `register_connector_v2(product="vcfa", version="9.0", impl_id="vcfa-rest", cls=VcfAutomationConnector)`
   call and queues `register_vcfa_typed_operations` onto the lifespan
   typed-op registrar list.
3. The registry's v2 table now resolves `("vcfa", "9.0",
   "vcfa-rest")` to `VcfAutomationConnector`. The G0.7 auto-shim's
   idempotency check (in `ensure_connector_class_registered`) no-ops on
   subsequent ingests against the same triple.
4. `run_typed_op_registrars()` (lifespan) invokes the registrar, which
   upserts the five `typed_ops.VCFA_TYPED_OPS` descriptors — no ingest
   needed, so the audited read surface works on a fresh boot.

### Vhost routing (load-bearing)

VCFA 9.x enforces strict `Host:` header matching — the consumer wrapper
(`scripts/vcf-automation.sh`) uses `curl --resolve fqdn:443:<ip>` to override
DNS while keeping the FQDN in the request line. In httpx terms, the
connector's per-target `AsyncClient` is built with
`base_url=https://<fqdn-or-host>`; the cleanest equivalent of `--resolve` is
to use the FQDN as the URL host and rely on operator-side DNS resolution.

The decision tree in `VcfAutomationConnector._base_url`:

1. `target.fqdn` is set → base URL host is the FQDN.
2. `target.fqdn` is unset and `target.host` is an IP literal (IPv4 or IPv6,
   bracket-wrapped accepted) → raise `VcfAutomationConfigurationError` with
   a clear message naming the target and pointing operators at the `--fqdn`
   / `fqdn:` configuration knob. Without this guard every path returns 404
   with empty body post-login.
3. `target.fqdn` is unset and `target.host` is itself an FQDN → use
   `target.host` as the URL host (the right vhost is already on the wire).

`fingerprint()` catches the configuration error and reports a structured
`reachable=False` with `extras["error"]` rather than bubbling the exception
to the dispatcher.

### Plane selection by path prefix

`VcfAutomationConnector.auth_headers(target, operator, *, path=...)` is
keyword-only on `path` and **requires** the path argument — a `None`
default raises `VcfAutomationConfigurationError` because this connector has
no plane-agnostic header set. The base `HttpConnector._request_json` /
`_post_json` callers don't forward `path`, so the connector overrides both
transports (`_request_json` / `_post_json`) to thread the path through
before resolving headers.

Plane classification (`_plane_for_path`):

- `/iaas/api/*` → tenant plane.
- Everything else (`/cloudapi/*`, `/api/*`) → provider plane.

The provider plane Bearer JWT authenticates both `/cloudapi/*` and the
classic `/api/*` surface — only the `Accept` media type differs (#517 in
the consumer repo, validated 2026-05-17):

- `/cloudapi/*` → `Accept: application/json;version=9.0.0`
- `/api/*` → `Accept: application/*+json;version=40.0`

### Provider-plane session establishment

1. `auth_headers(..., path="/cloudapi/...")` resolves the plane to
   `"provider"` and calls `_provider_session_token(target)`.
2. The lock-protected token cache fast-paths a cached JWT.
3. On cache miss: credentials are loaded. When `target.provider_secret_ref`
   is set, the loader is invoked with the override path (typical: a separate
   Vault entry for the VCFA-local `admin@System` password); otherwise the
   default `target.secret_ref` pair is used for both planes.
4. The Basic-auth username is `target.provider_username` verbatim when set
   (typical: `admin@System`), otherwise the legacy fallback
   `f"{creds['username']}@{target.domain or 'System'}"`.
5. `POST /cloudapi/1.0.0/sessions/provider` with HTTP Basic and
   `Accept: application/json;version=9.0.0`. A 2xx response carries
   `X-VMWARE-VCLOUD-ACCESS-TOKEN` (a JWT) — the connector caches it under
   `target.name`. Absence of the header on a 2xx response surfaces as a
   `RuntimeError` rather than caching an empty token.
6. `auth_headers` returns `{"Authorization": f"Bearer {jwt}", "Accept": <path-aware>}`.

### Tenant-plane session establishment

1. `auth_headers(..., path="/iaas/api/...")` resolves the plane to
   `"tenant"` and calls `_tenant_session_token(target)`.
2. The lock-protected token cache fast-paths a cached token.
3. On cache miss: credentials are loaded from `target.secret_ref` (the
   tenant plane does NOT honour `provider_secret_ref`).
4. `POST /iaas/api/login` with JSON body
   `{"username": ..., "password": ..., "domain"?: ...}` (the `domain` key
   is added when `target.domain` is set) and
   `Accept: application/json` + `Content-Type: application/json`.
5. The response body is `{"token": "..."}` — the token is cached under
   `target.name`. Missing / empty `token` field on a 2xx response surfaces
   as `RuntimeError`.
6. `auth_headers` returns `{"Authorization": f"Bearer {token}", "Accept": "application/json"}`.

### 401 → re-login + retry-once (per plane, independent)

`VcfAutomationConnector._request_json` (idempotent verbs) and
`_post_json` (POST) share a common `_do_request_with_retry` helper:

1. Build headers via `auth_headers(..., path=path)` (lazy login on first use).
2. Fire the request through the per-target `AsyncClient`.
3. On HTTP 401: invalidate the relevant plane's cache via
   `_invalidate_plane(target, plane)`, refresh headers (re-login on demand),
   retry once.
4. A second 401 surfaces as `RuntimeError` naming the target and the plane
   — consumer wrapper posture: re-login once on session-expiry, not a
   retry loop. Hammering VCFA's audit log on a misconfigured credential
   pair is the failure mode this rule guards against.
5. The per-plane lock means a tenant-plane 401 doesn't block in-flight
   provider-plane traffic and vice versa.

### Fingerprint + probe

- `fingerprint(target)` issues both unauthenticated version probes in series
  through the per-target httpx client:
  - Provider: `GET /api/versions` — returns vCD-API version XML. The
    connector reads the status only; XML parsing for the "latest
    non-deprecated" string lives in the consumer wrapper, which the
    operator-facing CLI fingerprint surfaces. What we record is that the
    appliance responded 2xx on the canonical provider probe.
  - Tenant: `GET /iaas/api/about` — returns JSON
    `{"latestApiVersion": "...", "supportedApis": [...]}`. The connector
    reads `latestApiVersion` into the result's `version` field.
- Both probes must succeed for `reachable=True`. A failure on either plane
  surfaces as `reachable=False` with `extras["failed_plane"]` naming the
  offender and `extras["error"]` carrying the exception class + message.
  Vhost mis-configuration (IP host with no `fqdn`) is caught at
  `_http_client` construction and reported as the structured failure too.
- `probe(target)` delegates to `fingerprint` — both unauth probes already
  cover reachability across both planes, so a separate path would add
  round-trip cost without changing the boolean `ok`.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:vcfa-rest-connector-shim"`) and delegates
to `meho_backplane.operations.dispatch` with `connector_id="vcfa-rest-9.0"`.
Pre-G0.6 chassis routes reach the dispatcher through this shim; post-G0.6
callers (the `/api/v1/operations/call` route, MCP `call_operation`, and the
`meho vcf-automation …` CLI verbs added in #840) construct a real `Operator`
and call `dispatch` themselves.

### Shutdown

`aclose()` clears both `self._provider_tokens` and `self._tenant_tokens`
under their respective locks (no server-side session revoke is issued —
VCFA's session has an idle timeout, and a per-target network call during
lifespan shutdown is more risk than benefit) and delegates to
`HttpConnector.aclose()` which closes every per-target httpx client.

### Operator-review curation (G3.6-T11 #836)

`apply_vcfa_core_curation(review_service, *, tenant_id)` is the
post-ingest helper that drives the substrate so exactly the 6 curated
ops in `VCFA_CORE_OPS` end `is_enabled=True` and the 5 curated groups
in `VCFA_CORE_GROUPS` end `review_status='enabled'`. (This path applies
only to the ingested-curation browse remainder; the audited read set is
served typed and needs no curation.) The control flow mirrors
`apply_nsx_core_curation`:

1. `ReviewService.get_review_payload("vcfa-rest-9.0", tenant_id)` loads
   the post-ingest state.
2. For each non-core op in a curated group, `ReviewService.edit_op(...,
   is_enabled=False)` writes an operator-override audit row so the
   follow-on `enable_group` cascade skips it.
3. `ReviewService.edit_group` lands the curated `name` +
   plane-named `when_to_use`; `ReviewService.enable_group` flips
   `review_status='enabled'` and cascades `is_enabled=True` only to
   the 6 core ops.
4. `ReviewService.edit_op(..., llm_instructions=…)` lands the per-op
   guidance blob on each of the 6 curated ops.

The helper is safe to re-run (end-state idempotent) but emits redundant
audit rows on re-runs — the intended posture is one-shot per ingest.
The ingestion + operator-review wiring expects both VCFA specs
(`vcf-automation-9.0/cloudapi.yaml` + `vcf-automation-9.0/iaas.yaml`)
to be ingested under the same `(product, version, impl_id) = ("vcfa",
"9.0", "vcfa-rest")` triple **before** the helper runs — the same
multi-spec-merge contract `register_ingested_operations` implements
for vSphere's `vcenter.yaml` + `vi-json.yaml`. Each row carries a
`spec:cloudapi` or `spec:iaas` tag so operators can filter the review
payload per plane.

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool (inherited from
  `HttpConnector`); the connector calls `client.request` / `client.post`
  directly from `_do_request_with_retry` so it can thread plane-specific
  headers without rerouting through `_request_json`'s tenacity decorator.
  The connection-error / 5xx retry layer lives on the base method and
  applies to callers that use the base `_get_json` / `_post_json` paths
  (the dispatcher always uses the overridden ones here).
- **tenacity 9.x** — installed dependency; not in direct use on this
  connector's overrides (the per-plane 401 retry-once is the only retry
  layer). Inherited use of tenacity persists on the base `_request_json`.
- **pydantic 2.13.x** — `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models; the connector constructs them by
  keyword.
- **respx 0.23.x (test-only)** — the unit-test module mocks every request
  shape (both logins + both probes + all four 401-retry scenarios) without
  a network call.
- **structlog** — `vcf_automation_provider_session_established` and
  `vcf_automation_tenant_session_established` info events on first-use
  login per plane per target; no other emit points in this skeleton.

## Known issues

- Default credentials loader raises `NotImplementedError`. Production
  callers must inject `credentials_loader=...` on construction until the
  operator-context per-target Vault credential read is wired for this
  connector (tracked under open Goal #214). Mirrors the `vmware_rest` /
  `nsx` / `sddc_manager` precedents.
- The classic vCD `/api/versions` response is XML; the connector reads
  status only and does not parse "latest non-deprecated version" out of it.
  Operators who need that string call the wrapper directly; the curated
  ingest in #836 will route through the structured `/cloudapi/*` and
  `/iaas/api/*` paths instead.
- The VCFA tenant/consumption plane's only *vendor-published*
  machine-readable surface is the 8 **Swagger 2.0** fragments vendored
  under [`vmware/vra-sdk-go`
  `swagger/`](https://github.com/vmware/vra-sdk-go/tree/v0.6.5/swagger)
  (`vra-project.json` … `vra-iaas.json`); the provider/management plane
  ships no swagger artifact at all. The ingest parser is
  OpenAPI-3.x-only by decision (#2090, reaffirming #1532) and rejects
  native 2.0 with a structured `UnsupportedSpecError` naming the
  conversion on-ramp — convert with `swagger2openapi` /
  `converter.swagger.io` first, then ingest the 3.x output (see the
  ["Product ships only Swagger
  2.0"](../cross-repo/connector-ingestion.md#product-ships-only-swagger-20)
  runbook section, which uses VCFA as the worked example). This is
  **orthogonal to the curated 11-op read core**: `VCFA_CORE_OPS` is
  sourced from the OpenAPI-3.x `vcf-automation-9.0/cloudapi.yaml` +
  `iaas.yaml` documents, so lighting up the core never touches the
  vra-sdk-go 2.0 fragments — converting them only *widens* the surface
  beyond the curated core.
- `--resolve`-style DNS override (consumer-wrapper-only) has no direct
  httpx equivalent in the connector — operators are expected to make the
  appliance's FQDN resolvable on the meho-backplane host (typical: split-DNS
  for the management network) when the target uses the IP-host-plus-FQDN
  shape. A future enhancement could use httpx's transport-level resolver
  hook, but the v0.2 posture matches MEHO's standard "operator-owned DNS"
  assumption.

## References

- Issues: [G3.6-T10 #832](https://github.com/evoila/meho/issues/832)
  (skeleton — this Task); [G3.6-T11 #836](https://github.com/evoila/meho/issues/836)
  (dual-plane spec ingestion + read ops); [G3.6-T12 #840](https://github.com/evoila/meho/issues/840)
  (CLI verbs + E2E + onboarding doc).
- Swagger-2.0 on-ramp decision: [#2090](https://github.com/evoila/meho/issues/2090)
  (parser stays OpenAPI-3.x-only; convert vra-sdk-go fragments
  out-of-band — see Known issues above).
- Parent Initiative: [G3.6 #369](https://github.com/evoila/meho/issues/369).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- Adapter dependency: [G0.2 #223](https://github.com/evoila/meho/issues/223)
  (`HttpConnector`).
- Substrate: [G0.6 #388](https://github.com/evoila/meho/issues/388)
  (v2 registry), [G0.7 #389](https://github.com/evoila/meho/issues/389)
  (spec ingestion pipeline).
- Sibling task: [G3.6-T13 #841](https://github.com/evoila/meho/issues/841)
  — shared `connectors/_shared/vcf_auth.py` for vROps + vRLI + Fleet. The
  VCF Automation connector intentionally does NOT use this helper because
  its dual-plane shape doesn't fit the single-pair-of-creds pattern the
  helper was designed for.
- Precedent: `connectors/nsx/connector.py` (session-cookie + XSRF +
  401-retry-once); `connectors/sddc_manager/connector.py` (per-target
  credential cache, dispatch-shim shape); `connectors/vmware_rest/`
  (dual-spec ingestion shape — same `spec_source` tagging that #836 will
  apply to the provider + tenant plane specs);
  `connectors/adapters/http.py` (`HttpConnector`);
  `connectors/registry.py:108` (`register_connector_v2`).
- VCFA API references:
  https://developer.broadcom.com/xapis/vmware-cloud-foundation-automation-api/latest/
  (provider/cloudapi);
  https://developer.broadcom.com/xapis/aria-automation-api/latest/
  (tenant/iaas).
- Consumer wrapper this contract mirrors (authoritative):
  [`scripts/vcf-automation.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-automation.sh)
  — header comment + login blocks verified 2026-05-21.

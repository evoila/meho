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

**Typed reads (T5 #2305; deployment list #2839; deployment get
#2960).** VCFA ships **no
vendor OpenAPI spec** (the provider plane publishes none; the tenant
plane ships only Swagger 2.0 fragments the ingest parser rejects by
decision #2090), so there is nothing to ingest — the hand-curated
`core_ops` ingested-enable apparatus that once wrapped this gap was
dispatch-inert on a real deploy and was **retired in #2362**. The
**audited read set** (evoila/meho#2294: org/region list, provider
health, `/iaas/api/projects` + tenant `about`), plus the tenant
**deployment list** (#2839) and per-id **deployment detail** (#2960),
is served by `source_kind="typed"` ops
(`typed_ops.py`) that dispatch through the connector's own dual-plane
session with **zero catalog state**. Seven ops:

| op_id | plane | path |
|---|---|---|
| `vcfa.provider.org.list` | provider | `GET /cloudapi/1.0.0/orgs` |
| `vcfa.provider.region.list` | provider | `GET /cloudapi/vcf/regions` |
| `vcfa.provider.health` | provider | `GET /cloudapi/1.0.0/site` |
| `vcfa.tenant.project.list` | tenant | `GET /iaas/api/projects` |
| `vcfa.tenant.deployment.list` | tenant | `GET /iaas/api/deployments` |
| `vcfa.tenant.deployment.get` | tenant | `GET /iaas/api/deployments/{id}` |
| `vcfa.tenant.about` | tenant | `GET /iaas/api/about` |

The region list rides the `vcf/` cloudapi prefix, not the classic
`1.0.0/` one — VCFA 9.0 moved Region there and 404s the classic form
(repointed by the #2983 reconcile; rationale on the
`PROVIDER_REGIONS_PATH` constant). Every hand-coded `METHOD:/path` in
the connector is swept by the spec-reconcile lane
(`backend/tests/test_connectors_vcf_automation_spec_reconcile.py`,
#2983): the tenant-plane half asserts against the pinned Apache-2.0
`vra-iaas.json` on the spec shelf; the provider-plane half is an
evidenced exclusion (no pinnable wire spec exists — see the
`vcf-automation-9.0` entry in
`docs/decisions/spec-reconcile-guards-standard.md`).

Each op **declares the plane it rides**; `typed_ops._validate_typed_op_planes`
asserts at import time that the declared `plane` matches
`plane_for_path(op.path)`, so a drift fails the import rather than
surfacing as a misrouted HTTP 401. The detail read's `{id}` path
template is percent-encoded (empty safe set) by the handler at
substitution time — OpenAPI `style: simple` semantics, matching the
ingested dispatch path's `{var}` expansion. The `org create` write
(`POST /cloudapi/1.0.0/orgs`) is deliberately out of scope — a first
write on a read-only connector belongs in a G3.x-mold approval-gated
write-surface initiative. Blueprint listing and the provider
users list remain unconverted (initiative #2833 ranks them low tier) —
not part of the
"which deployments exist / which failed" answer this surface delivers.
The hand-curated ingested-enable apparatus (`core_ops.py` / `_core_data`)
those once lived under was retired in #2362; the wider ingested catalog
would stay browsable through the generic `ReviewService.enable_reads`
flow only where a convertible OpenAPI 3.x spec exists — which, for VCFA,
it does not.

`register_typed_operations` (a classmethod on the connector, queued onto
the lifespan registrar list via `register_vcfa_typed_operations` in
`__init__.py`) upserts the seven descriptors on startup — the same
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
- **`VCFA_TYPED_OPS` / `VcfaTypedOp` / `VCFA_TYPED_WHEN_TO_USE_BY_GROUP`**
  (`typed_ops.py`) — the seven typed read ops (T5 #2305; tenant
  deployment list #2839; tenant deployment get #2960) and their two
  per-plane groups (`vcfa-provider-reads`,
  `vcfa-tenant-reads`). Each `VcfaTypedOp` carries a `plane` + `path`;
  the module's `_validate_typed_op_planes()` cross-checks them at import
  so a declared-plane / path drift fails the import rather than
  surfacing as a misrouted 401.
- **`VCFA_PRODUCT` / `VCFA_VERSION` / `VCFA_IMPL_ID` /
  `VCFA_CONNECTOR_ID`** (`__init__.py`) — DB-side keys. Since #1814 the
  registry key `VcfAutomationConnector.product` was unified to `"vcfa"`,
  matching `VCFA_PRODUCT` (what `parse_connector_id("vcfa-rest-9.0")`
  extracts). All `endpoint_descriptor` and `operation_group` rows carry
  `product="vcfa"`. Relocated here from the retired `core_ops` /
  `_core_data` curation modules (#2358).

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
   upserts the seven `typed_ops.VCFA_TYPED_OPS` descriptors — no ingest
   needed, so the audited read surface works on a fresh boot.

### Vhost routing (load-bearing, #2863)

VCFA 9.x enforces strict `Host:` header matching — the consumer wrapper
(`scripts/vcf-automation.sh`) uses `curl --resolve fqdn:443:<ip>` to keep
the FQDN in the request line while dialling a specific IP. The connector
implements the same "connect by IP, route by vhost" posture in httpx
terms: the per-target `AsyncClient` **always** dials `target.host`
(`base_url=https://<host>[:port]` — the reachable NAT-alias IP, or an
FQDN), and `target.fqdn` is applied **per-request** as:

- the `Host:` header — via `_routing.vhost_header(fqdn, port)`, merged
  onto the auth headers on the data path, both plane logins, and both
  fingerprint probes; and
- the TLS SNI + certificate-verify name — via the `sni_hostname`
  request extension (the #2002 seam), so under `verify_tls=true` the
  presented cert is still verified against the FQDN, not the dialled IP.
  `VcfAutomationConnector._request_extensions` sets it with precedence
  `tls_server_name` > `fqdn` > derive-from-host.

Nothing FQDN-derived is baked into the pooled `AsyncClient`, so this is
compatible with the client-pool key (a per-request `fqdn` override never
serves a stale client), and the SSRF guard — which screens `target.host`
— now screens the address actually dialled. Before #2863 the FQDN was
put into `base_url`, which (a) made an appliance reachable only by a
NAT-alias IP structurally undialable, (b) could not disambiguate several
appliances sharing one vhost FQDN behind distinct aliases, and (c) let
the transport dial a host the SSRF guard never screened.

`compose_base_url` returns `https://{host}[:port]` for every valid shape
and refuses exactly one: an IP-literal `host` (IPv4 / IPv6, bracket-wrapped
accepted) with no `fqdn` — there is no vhost to present, so it raises
`VcfAutomationConfigurationError` naming the target + IP and pointing at
the `--fqdn` / `fqdn:` knob rather than emitting a post-login 404 storm.
An FQDN `host` with no `fqdn` is fine — httpx derives `Host:` from the
host, which already carries the right vhost.

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
  through the per-target httpx client. Each probe carries the vhost `Host:`
  header and TLS SNI the same way the data path does (#2863 closed the
  #2398 gap where the SNI seam reached only the two login POSTs):
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

### Operator-review curation (retired #2362)

The hand-curated ingested-enable apparatus G3.6-T11 (#836) once
added — `apply_vcfa_core_curation`, `VCFA_CORE_OPS` /
`VCFA_CORE_GROUPS`, `classify_vcfa_op` / `VCFA_PATH_RULES` — was
**retired in #2362** (T7 of #2358), along with the equivalent modules
in five sibling connectors. It had zero production call sites
(`apply_vcfa_core_curation` was invoked only by tests; real deploys
enable ingested ops through the generic review flow), and for VCFA it
was dispatch-inert regardless: the product ships no ingestible OpenAPI
3.x spec (see the **Typed reads** note above), so there was never a
catalog row to curate. The working read surface is the typed ops in
`typed_ops.py`; `ReviewService.enable_reads` remains the generic path
for ingested breadth on connectors that *do* publish a convertible spec.

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool (inherited from
  `HttpConnector`); the connector calls `client.request` / `client.post`
  directly from `_do_request_with_retry` so it can thread plane-specific
  headers without rerouting through `_request_json`'s tenacity decorator.
  The connection-error / 5xx retry layer lives on the base method and
  applies to callers that use the base `_get_json` / `_post_json` paths
  (the dispatcher always uses the overridden ones here). Verified against
  the pinned httpx 0.28.1 / httpcore 1.0.9: an explicit `Host` header
  survives onto the built request (httpx does not overwrite it from
  `base_url`) and the `sni_hostname` request extension drives httpcore's
  `server_hostname` for both TLS SNI and cert CN/SAN verification — the two
  facts the per-request vhost routing (#2863) relies on.
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
  Operators who need that string call the wrapper directly; the typed
  `vcfa.provider.health` / `vcfa.tenant.about` probes read the structured
  `/cloudapi/*` and `/iaas/api/*` version surfaces instead.
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
  **orthogonal to the typed read surface**: the working reads in
  `typed_ops.py` dispatch with zero catalog state, so they never touch
  the vra-sdk-go 2.0 fragments — converting those would only *widen* the
  ingested browse breadth, not change the typed reads.
- `--resolve`-style DNS override no longer needs split-DNS (#2863). The
  connector dials `target.host` directly and presents `target.fqdn` as the
  `Host:` header + TLS SNI per request, so the IP-host-plus-FQDN shape works
  without making the appliance's FQDN resolvable on the meho-backplane host.
  This is the `curl --resolve fqdn:443:<ip>` behaviour the consumer wrapper
  uses, done at the request layer rather than the transport resolver — and
  it is **required** (not merely convenient) when several appliances share
  one vhost FQDN behind distinct NAT aliases, since a single DNS name cannot
  map to N alias IPs. Operator note: a vcfa target must set `host` to the
  dialable address; a target whose `host` held a stale value while split-DNS
  carried the FQDN fails loud at first dispatch with the host named.

## References

- Issues: [G3.6-T10 #832](https://github.com/evoila/meho/issues/832)
  (skeleton — this Task); [G3.6-T11 #836](https://github.com/evoila/meho/issues/836)
  (dual-plane spec ingestion + read ops); [G3.6-T12 #840](https://github.com/evoila/meho/issues/840)
  (CLI verbs + E2E + onboarding doc).
- Connect-by-IP / route-by-vhost: [#2863](https://github.com/evoila/meho/issues/2863)
  (dial `target.host`, present `target.fqdn` per-request as `Host:` + TLS
  SNI; closes the #2398 probe/data-path SNI-seam gap) — builds on the
  `tls_server_name` SNI seam [#2002](https://github.com/evoila/meho/issues/2002)
  / [#2398](https://github.com/evoila/meho/issues/2398). The generic
  `net.http_probe` counterpart is split out as
  [#2896](https://github.com/evoila/meho/issues/2896).
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
  (dual-spec ingestion shape — the `spec_source` tagging precedent for a
  connector that ingests two specs under one triple);
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

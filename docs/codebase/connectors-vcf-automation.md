# Connector: vcf-automation (VCF Automation 9.0, dual-plane)

## Overview

The `vcf-automation` connector is the hand-rolled `HttpConnector` subclass that
dispatches VCF Automation REST operations under the
`(product="vcf-automation", version="9.0", impl_id="vcfa-rest")` registry
triple. G3.6-T10 (#832) shipped the skeleton — dual-plane auth (provider +
tenant), vhost / FQDN routing, fingerprint, probe, and the G0.6 dispatch shim.
G3.6-T11 (#836) will add dual-plane spec ingestion + operator-review curation;
G3.6-T12 (#840) will ship the CLI verb tree + recorded-fixture E2E.

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
  Class attributes: `product="vcf-automation"`, `version="9.0"`,
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

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_automation` triggers the
   module-level
   `register_connector_v2(product="vcf-automation", version="9.0", impl_id="vcfa-rest", cls=VcfAutomationConnector)`
   call.
3. The registry's v2 table now resolves `("vcf-automation", "9.0",
   "vcfa-rest")` to `VcfAutomationConnector`. The G0.7 auto-shim's
   idempotency check (in `ensure_connector_class_registered`) no-ops on
   subsequent ingests against the same triple.

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

`VcfAutomationConnector.auth_headers(target, raw_jwt, *, path=...)` is
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

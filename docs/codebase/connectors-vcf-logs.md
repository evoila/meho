# VCF Operations for Logs (vRLI) connector

## Overview

`backend/src/meho_backplane/connectors/vcf_logs/` ships
`VcfLogsConnector`, an `HttpConnector` subclass that authenticates
against vRLI 9.x with a **session-token Bearer** flow. The package
registers itself under the v2 connector registry triple
`(product="vcf-logs", version="9.0", impl_id="vrli-rest")` at import
time; the chassis lifespan's `_eager_import_connectors` discovers it
during startup.

This is one of the four VCF management-plane connectors landing under
Initiative #369 (G3.6). The wave is generic-ingested via G0.7 — this
Task ships only the **skeleton** (auth + fingerprint + probe + the
G0.6 dispatch shim). Operations arrive in #834 via spec ingestion of
`vcf-logs-9.0/openapi.yaml`.

The connector imports its auth scaffolding from the shared
`connectors/_shared/vcf_auth.py` module (#841 G3.6-T13) — vRLI is the
session-token consumer of that shared module's `vcf_session_login`
helper. The 401-retry-once loop wraps downstream calls and lives in
this connector module (not in the shared helper) because the
downstream paths differ per connector.

## Key types

- **`VcfLogsConnector(HttpConnector)`** —
  `connectors/vcf_logs/connector.py`. Hand-rolled session-token
  connector with per-target token cache + Vault-sourced
  service-account credentials + 401-driven re-login + retry-once.
- **`VcfLogsTargetLike`** — `connectors/vcf_logs/session.py`.
  Runtime-checkable Protocol extending the cross-connector
  `VcfTargetLike` with an optional `provider` field
  (`"Local"` / `"ActiveDirectory"` / `"vIDM"`). The concrete `Target`
  model satisfies it structurally once the column lands.
- **Re-exports of the shared module**: `VcfCredentialsLoader`,
  `load_credentials_from_vault` (default stub),
  `SessionLoginError` — callers should import these from
  `meho_backplane.connectors.vcf_logs` rather than reaching into
  `_shared`.

## Auth contract

**Verified against the consumer wrapper `scripts/vcf-logs.sh`
(2026-05-21 snapshot)** in `evoila-bosnia/claude-rdc-hetzner-dc` and
the vRLI 9.x REST API documentation. The wrapper is the authoritative
contract for the field shapes the appliance actually accepts.

### Login round-trip

```http
POST /api/v2/sessions
Content-Type: application/json
Accept: application/json

{"username": "<svc-account>", "password": "<…>", "provider": "Local"}
```

- `provider` field defaults to `"Local"` when the target leaves it
  unset (`vcf-logs.sh` line 95). `"ActiveDirectory"` and `"vIDM"` are
  the documented alternatives; v0.2 supports `Local` + `AD` only.
- Response body shape: `{"sessionId": "<token>", "ttl": <seconds>}`
  (`vcf-logs.sh` line 142 extracts `.sessionId`).

### Downstream calls

```http
GET /api/v2/<path>
Authorization: Bearer <sessionId>
Content-Type: application/json
Accept: application/json
```

The connector caches the `sessionId` per `target.name` in
`_session_tokens`; subsequent `auth_headers()` calls reuse the cached
value without a fresh login.

### 401 retry-once

`_get_json_with_session_retry()` wraps `HttpConnector._get_json` and
handles the session-expiry case:

1. Issue the GET with the cached Bearer header.
2. On HTTP 401 → invalidate the cached `sessionId` (credentials cache
   untouched — the 401 means the session expired, not that the creds
   are wrong) and re-login.
3. Retry the GET once with the fresh token.
4. If the retry also 401s → raise `RuntimeError` naming the target.

This is the same posture the NSX precedent established (re-login once,
not a retry loop) — a misconfigured credential pair fails fast instead
of hammering vRLI's audit log.

### Fingerprint + probe

`fingerprint()` issues an **unauthenticated** `GET /api/v2/version`.
The wrapper's probe mode auths first defensively, but the appliance
accepts the version call without a session — the connector takes the
cleaner unauthenticated path so a vRLI with valid TLS but broken
credentials still reports a reachable fingerprint.

Response shape (mirroring the wrapper's probe output):

```json
{
  "version": "9.0.0",
  "build": "21761695",
  "vendor": "vmware",
  "product": "vcf-logs",
  "extras": {
    "release_name": "VMware Aria Operations for Logs 9.0",
    "version_full": "9.0.0.0.21761695",
    "patch": "0"
  }
}
```

The version-string parsing matches `vcf-logs.sh` lines 226-251: the
appliance reports `version` as a dot-separated tuple
(e.g. `"9.0.0.0.21761695"`); `parts[0:3]` is the public version,
`parts[3]` is the patch, `parts[4]` is the build.

`probe()` delegates to `fingerprint()` and returns
`ProbeResult(ok=True)` when reachable, otherwise
`ProbeResult(ok=False, reason=<error>)`.

## Auth model gating

`auth_headers()` rejects any `auth_model` other than
`shared_service_account` (or `None` for pre-G0.3
column-not-yet-populated targets) with `NotImplementedError` naming
both the target and the requested mode. Per-user and impersonation
modes are deferred to v0.2.next — same posture vSphere / NSX / VCF
Automation take.

## Control flow

```text
caller wants to issue an authenticated GET:
  ├─ _get_json_with_session_retry(target, path, raw_jwt)
  │    ├─ _get_json(target, path)
  │    │    └─ HttpConnector._request_json (retry on 5xx / conn err)
  │    │         ├─ auth_headers(target, raw_jwt)
  │    │         │    └─ _session_token(target)
  │    │         │         ├─ _credentials.get(target)
  │    │         │         │    └─ user-injected or Vault loader
  │    │         │         └─ vcf_session_login(...)
  │    │         │              └─ POST /api/v2/sessions
  │    │         └─ client.request("GET", path, ...)
  │    │              └─ on 200 → return resp.json()
  │    │              └─ on 401 → raise HTTPStatusError
  │    └─ on HTTPStatusError(401):
  │         ├─ _invalidate_session(target)       # drop cached sessionId
  │         └─ retry _get_json(...) once
  │              └─ on 401 again → RuntimeError "after refresh"
```

The credentials cache (`_credentials`, a shared `CredentialsCache`
keyed on `target.name`) is touched once per target lifetime; the
session-token cache (`_session_tokens`) is touched once per
target-session lifetime (initial login + any 401-driven re-login).
Both caches are flushed on `aclose()`.

## Dependencies

- `connectors/_shared/vcf_auth.py` (#841 G3.6-T13) — provides
  `CredentialsCache`, `vcf_session_login`, `is_acceptable_auth_model`,
  `SessionLoginError`, the default `load_credentials_from_vault` stub.
- `connectors/adapters/http.py` — `HttpConnector` base with the
  per-target httpx client pool, tenacity retry decorator on
  `_request_json`, and `aclose()` plumbing.
- `connectors/registry.py` — `register_connector_v2` for the
  module-import-time registration.
- `connectors/schemas.py` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.

External: `httpx>=0.27` (Bearer header + `AsyncClient`), `structlog`
(observability), `respx>=0.21` (test mocks only).

## Known issues

- The default credentials loader
  (`_shared.vcf_auth.load_credentials_from_vault`) is a stub that
  raises `NotImplementedError`; production deploys must inject a
  custom loader on connector construction until the operator-context
  Vault read path is wired for the VCF management-plane connectors
  (tracked under Goal #214).
- No proactive token refresh. vRLI's session has a documented TTL
  (default 30 days but operator-tunable); the connector relies on the
  401-retry layer to handle expiry. Acceptable in v0.2; revisit if
  operator-side cache TTLs drop below the round-trip timing.
- No DELETE-revoke on `aclose()` — a per-target network call during
  lifespan shutdown is more risk than benefit (same posture NSX
  takes). Revoke-on-close is v0.2.next.

## References

- Task: <https://github.com/evoila/meho/issues/830>
- Parent initiative: <https://github.com/evoila/meho/issues/369>
- Parent goal: <https://github.com/evoila/meho/issues/214>
- Shared auth module: `connectors/_shared/vcf_auth.py` (#841)
- vRLI API:
  <https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/>
- Consumer wrapper:
  <https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-logs.sh>
- Sibling skeleton precedent: NSX (`connectors/nsx/`) for the
  session-token + 401 retry-once pattern; VCF Automation
  (`connectors/vcf_automation/`) for the most recent VCF management-
  plane skeleton.

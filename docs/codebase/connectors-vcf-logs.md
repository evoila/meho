# VCF Operations for Logs (vRLI) connector

## Overview

`backend/src/meho_backplane/connectors/vcf_logs/` ships
`VcfLogsConnector`, an `HttpConnector` subclass that authenticates
against vRLI 9.x with a **session-token Bearer** flow. The package
registers itself under the v2 connector registry triple
`(product="vrli", version="9.0", impl_id="vrli-rest")` at import
time; the chassis lifespan's `_eager_import_connectors` discovers it
during startup. The registered `product` is the dispatch-canonical
token `parse_connector_id("vrli-rest-9.0")` derives from the
`vrli-rest` impl_id, so a target carrying the natural `product="vrli"`
spelling resolves this hand-rolled connector rather than an auto-shim
(#1798).

This is one of the four VCF management-plane connectors landing under
Initiative #369 (G3.6). The wave is generic-ingested via G0.7 — this
Task ships only the **skeleton** (auth + fingerprint + probe + the
G0.6 dispatch shim). Operations arrive in #834 via spec ingestion of
`vcf-logs-9.0/openapi.yaml`.

The connector imports its auth scaffolding from the shared
`connectors/_shared/vcf_auth.py` module (#841 G3.6-T13) — vRLI is the
session-token consumer of that shared module's `vcf_session_login`
helper. The session-expiry retry-once loop (440 or 401) wraps downstream
calls and lives in this connector module (not in the shared helper)
because the downstream paths differ per connector.

### Profile-derived auth + fingerprint (#1974, capstone of #1965)

The connector's declarative auth + fingerprint surfaces are now sourced
from the reviewed `VRLI_EXECUTION_PROFILE`
(`connectors/vcf_logs/profile.py`) rather than hand-coded literals — the
pilot that proves an `ExecutionProfile` can serve a shipped bespoke
connector's auth + fingerprint. The single declaration drives both the
typed connector and a profiled connector:

- the **session-create path** (`/api/v2/sessions`) comes from the
  profile's `session_login` named scheme spec (#1970);
- the **version endpoint** (`/api/v2/version`) and the `(public, build)`
  split come from the profile's fingerprint recipe — the `vrli_five_part`
  named splitter (#1972);
- the **session-expiry status set** `{401, 440}` is the profile's
  `expiry_statuses` (#1973), the one frozenset the retry seam narrows.

Two surfaces stay typed-only because the declarative profile cannot model
them: the per-target `provider` (`ActiveDirectory` / `vIDM`; the
`session_login` scheme hardcodes `"Local"`) and the fingerprint `extras`
(`release_name` / `version_full` / `patch`). The `ResultHandle`
large-result path is the connector-agnostic JSONFlux dispatch mechanism
(`connectors/schemas.ResultHandle` + `operations/jsonflux_reducer`), not
connector code, and is untouched. Per-method dispatch parity between the
typed and profiled paths is proven in
`tests/integration/test_connectors_vrli_profile_parity.py`.

## Key types

- **`VcfLogsConnector(HttpConnector)`** —
  `connectors/vcf_logs/connector.py`. Hand-rolled session-token
  connector with per-target token cache + Vault-sourced
  service-account credentials + session-expiry-driven re-login + retry-once
  (440 or 401).
- **`VcfLogsTargetLike`** — `connectors/vcf_logs/session.py`.
  Runtime-checkable Protocol extending the cross-connector
  `VcfTargetLike` with an optional `provider` field
  (`"Local"` / `"ActiveDirectory"` / `"vIDM"`). The concrete `Target`
  model satisfies it structurally once the column lands.
- **`VRLI_CORE_OPS` / `VRLI_CORE_GROUPS` / `apply_vrli_core_curation`** —
  `connectors/vcf_logs/core_ops.py` (#834 G3.6-T5). The
  operator-review metadata + driver for the read-only v0.5 core
  (7 ops across 5 groups: `vrli.about`, `vrli.event.query`,
  `vrli.aggregated.query`, `vrli.field.list`, `vrli.host.list`,
  `vrli.content.pack.list`, `vrli.alert.list`). The path-prefix
  classifier `classify_vrli_op` rejects non-`GET` methods so write
  ops never land under a curated group — same shape Harbor + NSX
  use.
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

### Session-expiry retry-once (440 or 401)

`_get_json_with_session_retry()` wraps `HttpConnector._get_json` and
handles the session-expiry case. vRLI signals an expired session two
ways and the connector recovers from both
(`_SESSION_EXPIRED_STATUSES = {401, 440}`):

- **`440`** — vRLI's own `trait.authenticated.440`: *"the session ID has
  expired; obtain a new session ID from `/api/v2/sessions`"* (carried by
  117 endpoints in the spec). This is the case that bites in practice:
  vRLI idle-expires the in-memory session, so the call after an idle gap
  returns 440. **This is the recoverable case** — re-login fixes it.
- **`401`** — `trait.authenticated.401`: missing/invalid `Authorization`
  header or session ID.

The loop:

1. Issue the GET with the cached Bearer header.
2. On HTTP 440 **or** 401 → invalidate the cached `sessionId`
   (credentials cache untouched — a 440/401 means the session expired or
   was rejected, not that the creds are wrong) and re-login.
3. Retry the GET once with the fresh token.
4. If the retry also returns 440/401 → raise `RuntimeError` naming the
   target and the status.

This is the same posture the NSX precedent established (re-login once,
not a retry loop) — a misconfigured credential pair fails fast instead
of hammering vRLI's audit log.

Before #1909 the trigger keyed strictly on `401`, so a 440 fell straight
through unretried: the first call after a backplane start worked (fresh
cached session), vRLI idle-expired the session, and every subsequent call
returned 440 until a backplane restart cleared the in-memory token cache —
breaking any scheduled / long-running vRLI consumer. The
dispatcher-side classification of 440 → structured `connector_auth_failed`
(#1804, `operations/_errors.py`) only fixed the *diagnosability* of the
flat error; the recovery (re-login on 440) lives here in the
session-retry.

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
  "product": "vrli",
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
  ├─ _get_json_with_session_retry(target, path, operator)
  │    ├─ _get_json(target, path, operator=operator)
  │    │    └─ HttpConnector._request_json (retry on 5xx / conn err)
  │    │         ├─ auth_headers(target, operator)
  │    │         │    └─ _session_token(target)
  │    │         │         ├─ _credentials.get(target)
  │    │         │         │    └─ user-injected or Vault loader
  │    │         │         └─ vcf_session_login(...)
  │    │         │              └─ POST /api/v2/sessions
  │    │         └─ client.request("GET", path, ...)
  │    │              └─ on 200 → return resp.json()
  │    │              └─ on 440 / 401 → raise HTTPStatusError
  │    └─ on HTTPStatusError(440 or 401):    # _SESSION_EXPIRED_STATUSES
  │         ├─ _invalidate_session(target)       # drop cached sessionId
  │         └─ retry _get_json(...) once
  │              └─ on 440 / 401 again → RuntimeError "after refresh"
```

The credentials cache (`_credentials`, a shared `CredentialsCache`
keyed on `target.name`) is touched once per target lifetime; the
session-token cache (`_session_tokens`) is touched once per
target-session lifetime (initial login + any session-expiry-driven
re-login on 440 or 401). Both caches are flushed on `aclose()`.

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
  (default 30 days but operator-tunable) and also idle-expires; the
  connector relies on the session-expiry retry layer (440 or 401) to
  recover on demand rather than refreshing ahead of time. Acceptable in
  v0.2; revisit if operator-side cache TTLs drop below the round-trip
  timing.
- No DELETE-revoke on `aclose()` — a per-target network call during
  lifespan shutdown is more risk than benefit (same posture NSX
  takes). Revoke-on-close is v0.2.next.

## References

- Task: <https://github.com/evoila/meho/issues/830>
  (skeleton + auth) and
  <https://github.com/evoila/meho/issues/834>
  (spec ingestion + operator-review curation)
- Parent initiative: <https://github.com/evoila/meho/issues/369>
- Parent goal: <https://github.com/evoila/meho/issues/214>
- Canary runbook: [`docs/cross-repo/g36-vrli-canary.md`](../cross-repo/g36-vrli-canary.md)
- Shared auth module: `connectors/_shared/vcf_auth.py` (#841)
- vRLI API:
  <https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/>
- Consumer wrapper:
  <https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-logs.sh>
- Sibling skeleton precedent: NSX (`connectors/nsx/`) for the
  session-token + 401 retry-once pattern; VCF Automation
  (`connectors/vcf_automation/`) for the most recent VCF management-
  plane skeleton.
- CLI verb tree (G3.6-T6 #838):
  [`cli/internal/cmd/vcf-logs/`](../../cli/internal/cmd/vcf-logs/) —
  thin Cobra wrappers over `POST /api/v1/operations/call` with
  `connector_id="vrli-rest-9.0"` pre-baked, mirroring the NSX /
  SDDC Manager alias-verb pattern.
- Operator-facing recipe (G3.6-T6 #838):
  [`docs/cross-repo/vcf-logs-onboarding.md`](../cross-repo/vcf-logs-onboarding.md).
- End-to-end recorded-fixture coverage (G3.6-T6 #838):
  [`backend/tests/test_connectors_vcf_logs_e2e.py`](../../backend/tests/test_connectors_vcf_logs_e2e.py)
  — exercises all 7 ops through the full dispatcher, the
  session-establish + session-expiry retry-once (440 and 401) +
  second-expiry-fails paths via `_get_json_with_session_retry`, the
  audit-row contract, and the JSONFlux handle path on
  `vrli.event.query`.

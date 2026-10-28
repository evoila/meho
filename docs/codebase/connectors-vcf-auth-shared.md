# Shared VCF management-plane auth helpers

## Overview

`backend/src/meho_backplane/connectors/_shared/vcf_auth.py` is the
cross-connector module the three skeleton VCF management-plane connectors
(VCF Operations / vROps #829, VCF Operations for Logs / vRLI #830, VCF
Fleet #831) import for their auth scaffolding. G3.6-T13 (#841) landed it
ahead of the three skeletons so each connector imports common helpers
instead of duplicating four copies of the same code (Harbor / NSX / SDDC
Manager each carry their own inlined copy — those stay; migrating them is
opportunistic later refactor only).

VCF Automation (#832) is **not** a consumer — its dual-plane auth
(provider Basic → `X-VMWARE-VCLOUD-ACCESS-TOKEN` JWT + tenant JSON login
→ `{token: ...}` + vhost routing via `--fqdn`) is bespoke and stays in
the Automation connector module.

`connectors/_shared/vcfa_session.py` (Task #5057) is the shared VCF
Automation session login for VCF 9 products that sign people in through
VCF Automation. See "VCF Automation session login" below. The
Orchestrator connector (`extras.login: vcfa-session`) is its first user;
other VCF 9 connectors can switch to it in their own Tasks.

The recorded-fixture refresh tool at
`backend/tests/fixtures/vcf/refresh.py` is documented separately in
`docs/cross-repo/vcf-fixture-refresh.md` (the operator-facing recipe).

## Key types

- **`basic_auth_header(username, password) -> str`** — returns the
  `Authorization: Basic <b64>` header value. Lifted verbatim from
  Harbor's `_basic_auth_header`.
- **`is_acceptable_auth_model(value) -> bool`** — accepts the
  `AuthModel.SHARED_SERVICE_ACCOUNT` enum member, its string value, or
  `None` (pre-G0.3 column-not-yet-populated sentinel); rejects
  everything else. Same predicate Harbor / NSX / SDDC Manager use.
- **`VcfTargetLike`** — runtime-checkable Protocol with fields `name`,
  `host`, `port`, `secret_ref`, `auth_model`. The concrete `Target`
  model in `meho_backplane.targets` (G0.3 #224) satisfies it
  structurally unchanged.
- **`VcfCredentialsLoader`** — async callable resolving a target to
  `{"username": ..., "password": ...}`. Injected on connector
  construction; tests / pre-G0.3 production deploys override the
  default Vault loader.
- **`load_credentials_from_vault`** — default loader, stubbed
  `NotImplementedError` until Goal #214 lands the operator-context
  per-target Vault credential read.
- **`CredentialsCache`** — small per-target cache around a
  `VcfCredentialsLoader`. Methods are all coroutines:
  `await get(target)`, `await invalidate(target)`, `await clear()` —
  each acquires the same internal `asyncio.Lock` so a concurrent
  rotation / connector-teardown cannot race an in-flight load (the
  load-once-per-target contract downstream consumers depend on).
  Property: `cached_targets`. Construct with
  `CredentialsCache(loader, product_label="vrops")` — the label is used
  in error messages so operators reading audit logs can attribute
  failures to a specific connector.
- **`SessionLoginError`** — typed `RuntimeError` subclass raised by
  `vcf_session_login` on non-2xx response, transport error, or
  structurally-invalid 2xx (empty token).
- **`vcf_session_login(client, path, *, username, password, target_name,
  payload_builder, token_extractor, request_headers)`** — POSTs
  credentials to *path*, returns the extracted session token, or raises
  `SessionLoginError` naming the target. The 401-retry-once loop around
  *downstream* calls stays in the consuming connector — only the login
  round-trip is shared.

- **`vcfa_session_login(client, *, username, password, org, target,
  request_extensions)`** (`vcfa_session.py`) — logs in to VCF Automation
  and returns a `VcfaSessionToken`.
- **`VcfaSessionToken`** — the token (`value`, left out of `repr`), the
  time it ends (`expires_at`, seconds since 1970), the `authorization`
  header value (`Bearer <token>`) and `needs_renewal(now)`.
- **`session_path(org)`**, **`token_expiry(token, now=...)`**,
  **`LOGIN_VCFA_SESSION`** (`"vcfa-session"`),
  **`RENEW_BEFORE_SECONDS`** (60), **`DEFAULT_LIFETIME_SECONDS`** (30
  minutes).

## Control flow

### VCF Automation session login (`vcfa_session.py`)

1. `POST /cloudapi/1.0.0/sessions` with HTTP Basic `<user>@<org>`. For
   the provider org `System` the path is
   `/cloudapi/1.0.0/sessions/provider`. With no org, the user name is
   sent as it is (it may already end with `@<org>`). The paths and the
   header name are the ones in `connectors/vcf_automation/_paths.py` and
   `_routing.py`.
2. The token comes back in the `X-VMWARE-VCLOUD-ACCESS-TOKEN` header. The
   caller sends it as `Authorization: Bearer <token>` to the product API.
3. The token is a JWT. The `exp` claim is read without checking the
   signature, only to plan the next login. No usable `exp`: the token is
   treated as ending after 30 minutes (the VCF Automation default for an
   idle session).
4. `needs_renewal()` is true 60 seconds before the end. The caller logs
   in again then, and once after a `401` (the dispatcher's
   `invalidate_session` hook).
5. HTTP 401 or 403 raises `ConnectorAuthError`, which the dispatcher
   turns into `connector_auth_failed`. The remediation says what to fix:
   the user, the org (`extras.org`, or that it is missing) and the
   secret that holds the password. Any other error status, or an answer
   without the token header, raises `SessionLoginError`.

Safety rules:

- The caller passes its pooled client for the target, so the password
  goes only to the target's host.
- The login goes straight to the client, not through the traced request
  path, so no flight-recorder trace holds the token. No log line, error
  or `repr` holds the token or the password.
- No cache and no retry in the helper. The connector keeps the token per
  tenant and target (`target_cache_key`).


### A vROps / Fleet consumer (HTTP Basic on every request)

1. Consumer constructs `CredentialsCache(loader, product_label="vrops")`
   in `__init__`.
2. `auth_headers(target, operator)` checks
   `is_acceptable_auth_model(target.auth_model)` → raises
   `NotImplementedError` if rejected.
3. `auth_headers` calls `await self._creds.get(target)` → returns
   `{"username": ..., "password": ...}` from the cache (loading on
   first use).
4. `auth_headers` returns
   `{"Authorization": basic_auth_header(creds["username"], creds["password"])}`.

### A vRLI consumer (session-login + 401 retry loop)

1. Consumer constructs `CredentialsCache(loader, product_label="vrli")`
   **and** a per-target `_session_tokens: dict[str, str]` cache.
2. `auth_headers(target, operator)` checks the auth-model gate.
3. `auth_headers` returns the cached session token under the product's
   token header name; on cache miss, calls a `_session_token(target)`
   helper that calls `vcf_session_login(...)` with the vRLI
   payload-builder (`{"username", "password", "provider": "Local"}`) and
   the `sessionId` header extractor.
4. The connector's own `_get_with_session_retry(target, path)` wraps
   the inherited GET: on 401 from the downstream call, it invalidates
   the cached session token and re-tries once. A second 401 raises.
   This loop lives in the consumer because the downstream path differs
   per connector.

## Dependencies

- **`httpx`** (>=0.27, 0.28.1 resolved) — the `AsyncClient` + `Response`
  + `HTTPStatusError` API.
- **`structlog`** — `vcf_credentials_loaded` and `vcf_session_established`
  events, both carrying `target` and `host`.
- **`meho_backplane.connectors.schemas.AuthModel`** — the boundary enum
  the gate predicate accepts.

## Known issues

- The default `load_credentials_from_vault` is a stub (`NotImplementedError`)
  pending Goal #214's operator-context per-target Vault credential read.
  Same shape as Harbor's and NSX's default loaders — they all flip to
  the live read in a single follow-up commit once #214 lands.
- The `vcf_session_login` helper does not retry transient network errors
  at this layer. The consumer's tenacity retry decorator on
  `HttpConnector._request_json` covers downstream calls; the session-login
  POST goes through `client.post` directly so failures surface cleanly.
  If a future product needs retries on the login round-trip itself, the
  helper grows an optional `retries=` parameter rather than a global
  decorator (different consumers have different idempotency expectations).

- Not proven: some appliances route by host name and answer 404 to a
  bare IP address. The first live check of the VCF Automation session
  login shows whether a `Host` header is needed (then reuse
  `vhost_header` from `connectors/vcf_automation/_routing.py`).

## References

- Task: https://github.com/evoila/meho/issues/841
- Task #5057: the VCF Automation session login (`vcfa_session.py`).
- Parent Initiative: https://github.com/evoila/meho/issues/369
- Parent Goal: https://github.com/evoila/meho/issues/214
- Recorded-fixture refresh: `docs/cross-repo/vcf-fixture-refresh.md`.
- Lift sources:
  `backend/src/meho_backplane/connectors/harbor/connector.py`
  (`_basic_auth_header`, `_is_acceptable_auth_model`, `_load_credentials`),
  `backend/src/meho_backplane/connectors/nsx/connector.py`
  (`_session_token`, session-create POST).

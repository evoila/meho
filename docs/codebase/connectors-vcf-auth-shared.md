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

## Control flow

### A vROps / Fleet consumer (HTTP Basic on every request)

1. Consumer constructs `CredentialsCache(loader, product_label="vrops")`
   in `__init__`.
2. `auth_headers(target, raw_jwt)` checks
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
2. `auth_headers(target, raw_jwt)` checks the auth-model gate.
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

## References

- Task: https://github.com/evoila/meho/issues/841
- Parent Initiative: https://github.com/evoila/meho/issues/369
- Parent Goal: https://github.com/evoila/meho/issues/214
- Recorded-fixture refresh: `docs/cross-repo/vcf-fixture-refresh.md`.
- Lift sources:
  `backend/src/meho_backplane/connectors/harbor/connector.py`
  (`_basic_auth_header`, `_is_acceptable_auth_model`, `_load_credentials`),
  `backend/src/meho_backplane/connectors/nsx/connector.py`
  (`_session_token`, session-create POST).

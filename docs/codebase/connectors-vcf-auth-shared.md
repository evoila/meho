# Shared auth helpers for appliance connectors

## Overview

`backend/src/meho_backplane/connectors/_shared/vcf_auth.py` holds the
auth helpers that several hand-written connectors share: HTTP Basic
headers, the auth-model check, a small per-target credential cache, a
session-login round trip, and the typed error the dispatcher turns into
`connector_auth_failed`.

The name comes from its first users, the VCF management-plane connectors.
Those connectors left the public tree with evoila's own connectors
(decision record `docs/decisions/2026-10-28-own-connectors-leave-the-public-tree.md`,
Task #3345) and ship in the private connector package. The module stays
public because core code and the public connectors import it:

- `operations/dispatcher.py` and `operations/_errors.py` turn
  `ConnectorAuthError` into the `connector_auth_failed` error envelope.
- `connectors/profiled.py` and `connectors/_shared/profile_auth.py` use
  the credential cache and the auth-model check for profile-backed
  connectors.
- The `azure`, `entra`, `keycloak` and `phpipam` connectors import the
  Basic header helper, the credential cache or the error class.
- `connectors/_shared/microsoft_login.py` builds on the same error class.

The test is `backend/tests/test_connectors_vcf_shared_auth.py`.

## Key types

- **`basic_auth_header(username, password) -> str`** — returns the
  `Authorization: Basic <b64>` header value.
- **`is_acceptable_auth_model(value) -> bool`** — accepts the
  `AuthModel.SHARED_SERVICE_ACCOUNT` enum member, its string value, or
  `None` (the column-not-yet-populated sentinel); rejects everything else.
- **`VcfTargetLike`** — runtime-checkable Protocol with fields `name`,
  `host`, `port`, `secret_ref`, `auth_model`. The concrete `Target`
  model in `meho_backplane.targets` satisfies it structurally.
- **`VcfCredentialsLoader`** — async callable resolving a target to
  `{"username": ..., "password": ...}`. Injected on connector
  construction; tests override the default loader.
- **`CredentialsCache`** — small per-target cache around a
  `VcfCredentialsLoader`. Methods are all coroutines: `await get(target)`,
  `await invalidate(target)`, `await clear()`. Each acquires the same
  internal `asyncio.Lock`, so a rotation or connector teardown cannot
  race an in-flight load. Property: `cached_targets`. Construct with
  `CredentialsCache(loader, product_label="phpipam")`; the label goes
  into error messages so an operator can attribute a failure to one
  connector. Since #5619 the cache records which caller read a secret, so
  a second caller reads the target's secret once before it may use the
  cached login.
- **`ConnectorAuthError`** — a `SessionLoginError` subclass a connector
  raises when the product rejects a login (HTTP 401 or 403). It carries the status code,
  a short `cause`, the target name and a remediation. The dispatcher turns
  it into `connector_auth_failed`.
- **`SessionLoginError`** — typed `RuntimeError` subclass raised by
  `vcf_session_login` on a non-2xx response, a transport error, or a
  structurally invalid 2xx (empty token).
- **`vcf_session_login(client, path, *, username, password, target_name,
  payload_builder, token_extractor, request_headers)`** — POSTs
  credentials to *path* and returns the extracted session token, or
  raises `SessionLoginError` naming the target. The 401-retry-once loop
  around downstream calls stays in the consuming connector; only the
  login round trip is shared.

## Control flow

### A connector that sends HTTP Basic on every request

1. The connector constructs `CredentialsCache(loader, product_label=...)`
   in `__init__`.
2. `auth_headers(target, operator)` checks
   `is_acceptable_auth_model(target.auth_model)` and raises
   `NotImplementedError` when the model is rejected.
3. `auth_headers` calls `await self._creds.get(target)`, which returns
   `{"username": ..., "password": ...}` from the cache (loading on first
   use).
4. `auth_headers` returns
   `{"Authorization": basic_auth_header(creds["username"], creds["password"])}`.

### A connector that logs in for a session token

1. The connector constructs a `CredentialsCache` and a per-target session
   token cache.
2. `auth_headers(target, operator)` checks the auth-model gate.
3. On a cache miss it calls `vcf_session_login(...)` with the product's
   payload builder and token extractor, and stores the token.
4. The connector's own request helper retries once after a `401`: it
   invalidates the cached token and logs in again. A second `401` raises
   `ConnectorAuthError`.

Safety rules:

- The caller passes its pooled client for the target, so the password
  goes only to the target's host.
- No log line, error or `repr` holds the token or the password.
- The helper keeps no retry loop of its own; the connector decides when
  to log in again.

## Dependencies

- `httpx` for the login round trip.
- `meho_backplane.connectors.schemas.AuthModel` for the auth-model check.

## Known issues

- The module name still says `vcf`. Renaming it touches every importer
  and is its own change.

## References

- `backend/src/meho_backplane/connectors/_shared/vcf_auth.py`
- `backend/tests/test_connectors_vcf_shared_auth.py`
- `docs/architecture/connector-auth.md`
- `docs/decisions/2026-10-28-own-connectors-leave-the-public-tree.md`

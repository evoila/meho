# `backend/` — backplane Python project

> Durable map of the backplane source tree at the chassis stage. Update
> in lock-step with code changes; stale entries are bugs.

## Overview

`backend/` houses the MEHO governance-layer backplane — a FastAPI
service that mediates every operation an AI agent runs against shared
infrastructure (policy gating, audit, federation, observability). At
this stage it exposes:

* The identity route at `/`.
* The public operator surfaces — `/healthz`, `/version`, `/ready` —
  backed by a pluggable readiness-probe registry. The chassis defaults
  to an empty registry that **fails closed on `/ready`** (Task #19);
  the lifespan hook now registers the Keycloak readiness probe
  (Task #22) so `/ready` flips green only when Keycloak's JWKS is
  reachable.
* Observability primitives — Prometheus metrics on `/metrics`,
  structured JSON logs to stdout via structlog, and a `request_id`
  correlation identifier propagated across every HTTP request via the
  request-context middleware (Task #20).
* Operator authentication — the `verify_jwt` FastAPI dependency
  validates `Authorization: Bearer <jwt>` against Keycloak's JWKS
  (cached, kid-rotation aware) and yields a frozen `Operator` model
  (Task #22). No protected routes are mounted yet; consumers land in
  G2.2-T3 (`/api/v1/health`).
* Vault forward-auth — the `vault_client_for_operator` async context
  manager performs a per-request JWT/OIDC login against Vault
  (`meho-mcp` role by default) using the operator's validated JWT,
  yields an authenticated `hvac.Client`, and revokes the issued token
  on exit (Task #23). The Vault readiness probe registered in lifespan
  flips `/ready` red when `/sys/health` is unreachable, sealed, or
  uninitialized.
* Federation-proof endpoint — `GET /api/v1/health` (auth-required) is
  the load-bearing integration point where the entire JWT → Vault
  chain runs on every call (Task #24). The route uses the
  `verify_jwt_and_bind` dependency wrapper, which delegates to
  `verify_jwt` and — on success — binds `operator_sub` into structlog
  contextvars. The handler then forwards the validated JWT to Vault
  via `vault_client_for_operator`, reads the test secret at
  `secret/meho/test/federation` (KV v2), and returns a structured
  JSON document with operator identity, Vault status, and a DB
  migration placeholder (`db.migrated = null` until G2.3). All Vault
  failure modes surface as structured fields on a 200 response — the
  smoke test never sees a 5xx from this endpoint.
* Federation-chain failure-mode test suite (Task #25) — comprehensive
  pytest coverage that proves the chain *fails safely*: every JWT
  failure shape (expired / wrong-aud / wrong-iss / tampered signature /
  tampered payload / unknown-key / wrong-algorithm including HS256 +
  `none` / missing kid / kid not in JWKS / missing sub) returns 401
  with a centrally-defined detail string; every Vault failure shape
  (unreachable / DNS / timeout / role-denied / 4xx / 5xx / sealed /
  uninitialized / standby / DR / perf-standby) maps cleanly onto the
  documented `VaultClientError` hierarchy or the readiness-probe
  status; the cross-axis matrix on `/api/v1/health` (auth-broken-only
  vs Vault-broken-only vs both) preserves the documented contracts;
  and the bearer token, the issued Vault token, and the Vault secret
  value never appear in any captured log line. The suite extends the
  `tests/conftest.py` autouse `_no_secret_leak_sweep` fixture across
  every test in `tests/` — a `Bearer\s+<long>` / `password\s*[=:]` /
  `secret\s*[=:]` / `token\s*[=:]` / `api_key\s*[=:]` /
  `Authorization:\s*Bearer\s+\S+` regex pass over `capfd` + `caplog`
  on every test exit, fail-closed when any pattern matches.

Database persistence lands progressively in subsequent G2.3 Tasks. The stack (FastAPI, Pydantic v2,
SQLAlchemy 2.x async, Alembic, structlog, prometheus_client, authlib
for JOSE) is locked by
[ADR 0004](https://github.com/evoila-bosnia/meho-internal/issues/13).

The project follows the modern src-layout
(`backend/src/meho_backplane/...`) so tests resolve only the installed
package and never the in-tree source — a guardrail against accidental
PYTHONPATH-leak imports.

## Key types

| Symbol | Location | Purpose |
| --- | --- | --- |
| `app` (`fastapi.FastAPI`) | `src/meho_backplane/main.py` | ASGI application instance consumed by uvicorn / k8s probes. Title and `version` are populated from `__version__` so OpenAPI metadata stays in lock-step with the package. |
| `__version__` (`str`) | `src/meho_backplane/__init__.py` | Single source of truth for the running app version. The pyproject `[project].version` field mirrors this constant; the `test_version_constant_matches_pyproject` test acts as a tripwire if the two drift. |
| `root` (route) | `src/meho_backplane/main.py` | `GET /` returning `{"name": "meho-backplane", "version": "<x>"}`. Identity smoke-probe; coexists with `/healthz`. |
| `metrics` (route) | `src/meho_backplane/main.py` | `GET /metrics` returning the Prometheus exposition format from the default registry (process / GC collectors + the `http_requests_total` counter). |
| `lifespan` | `src/meho_backplane/main.py` | FastAPI lifespan async context manager; calls `configure_logging()` once at startup. The yield/return shape leaves room for G2.2 / G2.3 teardown without restructuring. |
| `ProbeResult` (`dataclass`) | `src/meho_backplane/health.py` | Frozen record `(name, ok, detail)` returned by every readiness probe. Surfaced verbatim in the `/ready` response body. |
| `register_probe` / `run_probes` / `clear_probes` | `src/meho_backplane/health.py` | Public registry API. G2.2 (Vault, Keycloak) and G2.3 (DB migrations) call `register_probe` at startup; `clear_probes` is test-only. |
| `health.router` (`/healthz`, `/ready`) | `src/meho_backplane/health.py` | Liveness and readiness endpoints. `/healthz` is unconditional 200; `/ready` aggregates the probe registry and **fails closed on the empty default** (vacuous-truth trap explicitly guarded). |
| `version.router` (`/version`) | `src/meho_backplane/version.py` | Build identity. Reads `GIT_SHA` and `BUILD_DATE` env vars (injected via `docker build --build-arg`); falls back to `"unknown"` when unset or empty. `chart_version` is `None` until G2.5. |
| `configure_logging` | `src/meho_backplane/logging.py` | Configures structlog: `merge_contextvars` → `add_log_level` → `TimeStamper(iso, utc)` → `JSONRenderer`, writing to stdout. Idempotent. |
| `RequestContextMiddleware` | `src/meho_backplane/middleware.py` | Pure-ASGI middleware. Per request: extracts/mints a `request_id`, clears any leftover contextvars and binds the new `request_id`, mirrors it onto the `X-Request-Id` response header, increments `http_requests_total{method,path,status}`, emits one `request_completed` JSON log line with method / path / status / duration_ms (which inherits any contextvars bound during the request, including `operator_sub` from `verify_jwt_and_bind`). |
| `verify_jwt_and_bind` | `src/meho_backplane/middleware.py` | FastAPI dependency wrapper around `verify_jwt`. On successful validation, binds `operator_sub` (the JWT's `sub` claim) into structlog contextvars so every subsequent log line in the request scope carries operator identity automatically. Authenticated routes use `Depends(verify_jwt_and_bind)` instead of `Depends(verify_jwt)` directly. Lives alongside the middleware because `RequestContextMiddleware`'s request-entry `clear_contextvars` call is what guarantees the bound key does not leak across requests reusing the same asyncio task. |
| `SENSITIVE_HEADERS` | `src/meho_backplane/middleware.py` | `frozenset({b"authorization", b"cookie", b"x-api-key"})`. The middleware never logs the values of these headers; redaction is enforced by *not* logging request headers at all in v0.1, with a `tests/test_observability.py` regression test. |
| `HTTP_REQUESTS_TOTAL` | `src/meho_backplane/metrics.py` | Module-level `prometheus_client.Counter` registered against the default registry. Labels: `method`, `path`, `status`. `path` is the matched FastAPI route template when available, bounding label cardinality. |
| `render_metrics` | `src/meho_backplane/metrics.py` | Returns `(body, content_type)` for the `/metrics` route. Pins `text/plain; version=0.0.4; charset=utf-8` — the legacy Prometheus format every scraper accepts (`prometheus_client>=0.21` advertises 1.0.0 in `CONTENT_TYPE_LATEST`, but 0.0.4 stays universally compatible). |
| `Settings` / `get_settings` | `src/meho_backplane/settings.py` | Pydantic v2 model + `lru_cache`-singleton accessor for the Keycloak knobs (`KEYCLOAK_ISSUER_URL`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_JWKS_CACHE_TTL_SECONDS`, `KEYCLOAK_JWT_LEEWAY_SECONDS`) and the Vault knobs (`VAULT_ADDR`, `VAULT_OIDC_ROLE`, `VAULT_OIDC_MOUNT_PATH`, `VAULT_NAMESPACE`, `VAULT_TIMEOUT_SECONDS`). Tests reset via `get_settings.cache_clear()`. |
| `Operator` | `src/meho_backplane/auth/operator.py` | Frozen pydantic v2 model carrying validated claims (`sub`, `name`, `email`, `raw_jwt`). Returned by `verify_jwt`; consumed by every authenticated route from G2.2-T3 onward. `raw_jwt` is preserved verbatim for G2.2-T2's Vault forward-auth. |
| `verify_jwt` | `src/meho_backplane/auth/jwt.py` | FastAPI dependency: parses `Authorization: Bearer ...`, fetches/caches Keycloak's JWKS, validates signature + `iss` + `aud` + `exp` (with leeway), refreshes JWKS on a kid miss, and returns an `Operator`. Every failure mode collapses to a terse 401. |
| `keycloak_readiness_probe` | `src/meho_backplane/auth/jwt.py` | Synchronous probe registered with the readiness registry at app lifespan startup. Hits `{issuer}/.well-known/openid-configuration` then `jwks_uri`; failure detail surfaces only the exception class name to avoid leaking issuer URLs into 503 payloads. |
| JWKS cache | `src/meho_backplane/auth/jwt.py` (`_jwks_cache`, `_jwks_fetched_at`, `_jwks_lock`) | Module-level dict + monotonic-fetched timestamp + asyncio lock. TTL-bounded (default 5 min) and kid-rotation refreshed (one forced re-fetch per request on a kid miss). Single-worker design; v0.2 may move to Redis when multi-worker uvicorn is needed. |
| `vault_client_for_operator` | `src/meho_backplane/auth/vault.py` | Async context manager: builds an `hvac.Client` from settings, performs `client.auth.jwt.jwt_login(role, jwt, path)` against the configured mount path, yields the authenticated client, and revokes the issued token on exit (best-effort). Every blocking hvac call runs through `asyncio.to_thread` because hvac is `requests`-based and FastAPI does not auto-offload sync I/O inside `async def` callables. Per-request login by design (v0.1); v0.2 may add a per-operator cache. |
| `vault_readiness_probe` | `src/meho_backplane/auth/vault.py` | Synchronous probe registered with the readiness registry at app lifespan startup. Calls `client.sys.read_health_status(method='GET')` (unauthenticated) and classifies the response — `sealed=False`/`http_429`/`http_472`/`http_473` → ok; `sealed`/`uninitialized`/connection-error → not ok. Detail strings never echo the Vault URL or namespace. |
| `VaultClientError` / `VaultUnreachableError` / `VaultRoleDeniedError` | `src/meho_backplane/auth/vault.py` | Backplane-side exception hierarchy. Callers catch `VaultClientError` for a single error response shape, or one of the subclasses to map to specific HTTP statuses. The hierarchy lets consumers avoid importing `hvac` directly. |
| `api/v1/health.router` (`/api/v1/health`) | `src/meho_backplane/api/v1/health.py` | Authenticated federation-proof endpoint (Task #24). `GET` handler runs through `Depends(verify_jwt_and_bind)`, calls `vault_client_for_operator(operator)`, reads `secret/meho/test/federation` (KV v2), and returns `HealthResponse` (operator identity + vault status + db status). Vault unreachable / role denied / read failure surface as structured fields on a 200 response — never 5xx. |
| `HealthResponse` / `OperatorIdentity` / `VaultStatus` / `DbStatus` | `src/meho_backplane/api/v1/health.py` | Frozen pydantic v2 response models. `OperatorIdentity` deliberately excludes `raw_jwt` so the bearer token never appears in the response body. `DbStatus.migrated` is `None` until G2.3 wires Alembic. `VaultStatus.detail` carries only structured tokens (`version=N`, `read_failed: <ExcClass>`, `login_failed: <ExcClass>`) — no operator-controllable URL substrings. |
| `_no_secret_leak_sweep` | `tests/conftest.py` (autouse) | Pytest fixture that runs after every test in `tests/`, scanning `capfd`-captured stdout/stderr and `caplog` records for credential-shaped substrings (`Bearer <long>`, `password=`, `secret=`, `token=`, `api_key=`, `Authorization: Bearer …`). First match → `pytest.fail` with a redacted preview. The patterns live in `SECRET_LEAK_PATTERNS` for contributor extension; the targeted leak tests in `tests/test_secret_leak_checks.py` complement the always-on sweep with explicit assertions on the structlog `StringIO` buffers used by route-level tests. |

## Control flow

1. The container's `CMD` invokes
   `uvicorn meho_backplane.main:app --host 0.0.0.0 --port 8000`.
2. uvicorn imports `meho_backplane.main`, which constructs the
   `FastAPI` instance with the `lifespan` async context manager,
   wraps it in `RequestContextMiddleware`, and mounts the `health` and
   `version` routers via `include_router` alongside the inline `root`
   and `metrics` route handlers.
3. uvicorn opens the lifespan context: `configure_logging()` runs,
   pinning structlog's processor chain so every subsequent log line
   is JSON to stdout, and the Keycloak + Vault readiness probes are
   registered against the probe registry.
4. uvicorn binds to `:8000` and starts the ASGI event loop.
5. Each HTTP request enters `RequestContextMiddleware.__call__`,
   which:
   - extracts the incoming `X-Request-Id` header value (or mints a
     fresh `uuid4().hex`),
   - clears any leftover contextvars and binds `request_id`,
   - calls the wrapped FastAPI app,
   - on the first `http.response.start` ASGI message, appends
     `X-Request-Id` to the response headers and captures the status
     code,
   - increments `http_requests_total{method,path,status}` and emits
     a single `request_completed` log line with `duration_ms`.
6. The wrapped app dispatches each request to its route handler:
   - `GET /` → `root()` returns the identity dict.
   - `GET /healthz` → `healthz()` returns `{"status": "ok"}` with 200.
   - `GET /version` → reads `GIT_SHA` / `BUILD_DATE` env vars per
     request (cheap; no caching needed).
   - `GET /ready` → calls `run_probes()` and translates the aggregate
     into a 200 / 503 `JSONResponse`.
   - `GET /metrics` → returns the default registry's exposition text
     directly. The middleware still wraps it, so `/metrics` requests
     show up in the counter (under `path="/metrics"`).
   - `GET /api/v1/health` → resolves `Depends(verify_jwt_and_bind)`
     (which runs `verify_jwt` and binds `operator_sub` into
     contextvars on success), calls `vault_client_for_operator(op)`
     to forward the JWT to Vault and obtain a per-operator client,
     reads `secret/meho/test/federation`, and returns the
     `HealthResponse` document. The middleware's eventual
     `request_completed` log line inherits `operator_sub` because
     the binding lives in the same request-scoped contextvar context.

The FastAPI
[lifespan](https://fastapi.tiangolo.com/advanced/events/) hook is the
modern replacement for the deprecated `@app.on_event("startup")`
decorator; it currently performs only the structlog configuration but
is the right seam for the G2.2 Vault client and G2.3 SQLAlchemy
engine setup/teardown. Downstream readiness probes will be registered
from those lifespans: `register_probe("vault", check_vault)`.

## Dependencies

Pinned-floor declarations; exact versions resolved into `uv.lock`.

| Library | Floor | Why it's here |
| --- | --- | --- |
| `fastapi` | ≥ 0.110 | Web framework + OpenAPI 3.1 emission (per ADR 0004). |
| `uvicorn[standard]` | ≥ 0.30 | ASGI server with `httptools` / `websockets` extras. |
| `pydantic` | ≥ 2.6 | Pulled transitively by FastAPI; pinned explicitly so v1 can't be substituted. |
| `structlog` | ≥ 24.1 | JSON-to-stdout logging + `contextvars`-based `request_id` propagation. |
| `prometheus-client` | ≥ 0.20 | Default process / GC collectors + the `http_requests_total` counter exposed on `/metrics`. |
| `pydantic[email]` | ≥ 2.6 | Frozen `Operator` model uses `EmailStr`, which pulls `email-validator` via the `email` extra. |
| `authlib` | ≥ 1.3 | JWS / JWK / JWT primitives for Keycloak token verification. The `authlib.jose` namespace is deprecated in favour of `joserfc` (same maintainer, clean rewrite); migration to `joserfc` is tracked as a v0.2 candidate. |
| `httpx` | ≥ 0.27 | Async + sync HTTP client for the OIDC discovery doc and JWKS endpoint. (Also used as the `fastapi.testclient.TestClient` backend.) |
| `hvac` | ≥ 2.4 | Official HashiCorp Vault Python client (per ADR 0004). Sync-only, transitively pulls `requests` + `urllib3`; the backplane localises the sync surface inside `auth/vault.py` and wraps every call in `asyncio.to_thread` from the async context manager. No type stubs ship with the package, so `[tool.mypy.overrides]` whitelists `hvac.*` and `requests.*`. |
| (dev) `pytest` ≥ 8 | | Test runner. |
| (dev) `pytest-asyncio` ≥ 0.23 | | Async test support; `asyncio_mode = "auto"` in pyproject. |
| (dev) `cryptography` ≥ 42.0 | | RSA keypair generation in test fixtures (authlib pulls it transitively in production). |
| (dev) `respx` ≥ 0.21 | | httpx-native mock router used to stub Keycloak's discovery + JWKS endpoints in `tests/test_auth_jwt.py`. |
| (dev) `pytest-cov` ≥ 5.0 | | Line-coverage reporting; Task #25 acceptance criterion pins `auth/jwt.py` and `auth/vault.py` at >90% line coverage. |
| (dev) `ruff` ≥ 0.5 | | Lint + format. |
| (dev) `mypy` ≥ 1.10 | | Strict type checking. |

## Known issues

`/ready` returns 503 until every registered probe passes. After Task
#23 the lifespan hook registers both the Keycloak and Vault probes,
so a running app needs Keycloak's JWKS endpoint reachable **and**
Vault's `/sys/health` reachable + unsealed before `/ready` flips green.
Until DB migrations land in G2.3, the registry is otherwise complete
for the federation-chain dependency surface. Helm charts pointing
their kubernetes readiness probe at `/ready` before either dependency
is provisioned will see pods stay `NotReady`; that is the intended
contract.

Vault hvac calls are synchronous (the library is built on `requests`)
but the backplane is async-first. Every hvac call from
`auth/vault.py` runs through `asyncio.to_thread` to avoid blocking
the event loop. v0.2 may move to a native async Vault client when one
of acceptable maturity emerges, or to per-operator token caching to
reduce login pressure under higher load.

`authlib.jose` emits an `AuthlibDeprecationWarning` at first import,
recommending `joserfc` (the same maintainer's clean rewrite). The
warning shows up once in every pytest run because authlib's own
`authlib/deprecate.py` calls `warnings.simplefilter("always",
AuthlibDeprecationWarning)` at import time, overriding any nested
`warnings.catch_warnings()` context. We intentionally leave the
warning visible — it's the migration breadcrumb for the v0.2
`joserfc` switch — and the published API stays stable until
authlib 2.0.

The `Authorization` / `Cookie` / `X-API-Key` redaction guarantee in
`RequestContextMiddleware` is enforced *by omission*: the middleware
never logs request headers at all in v0.1. If a future Initiative
adds header logging, it must filter through `SENSITIVE_HEADERS`
explicitly — there is a regression test
(`tests/test_observability.py::test_sensitive_headers_never_leak_into_logs`)
that asserts no header value ever leaks, but the test only catches
the three named headers, so the contract relies on the omission
discipline rather than a denylist.

`prometheus_client>=0.21` exposes `CONTENT_TYPE_LATEST` as
`text/plain; version=1.0.0; charset=utf-8`. The `/metrics` route
intentionally pins `CONTENT_TYPE_PLAIN_0_0_4` instead, because
version 0.0.4 is supported by every Prometheus deployment in the
wild and Goal #11's acceptance criterion specifies it. When the
ecosystem catches up to the 1.0.0 format, the constant in
`metrics.py` is the one knob to turn.

## References

- ADR 0004 — Stack choice (Python backplane + Go CLI)
- Task #18 — this chassis bootstrap
- Task #19 — public health + version + readiness endpoints
- Task #20 — observability primitives (`/metrics`, structlog, middleware)
- Task #22 — Keycloak JWT validation + readiness probe
- Task #23 — Vault OIDC forward-auth client + readiness probe
- Task #24 — Federation-proof `/api/v1/health` + operator-identity propagation
- Task #25 — Federation-chain failure-mode test suite + always-on secret-leak sweep
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [FastAPI lifespan API](https://fastapi.tiangolo.com/advanced/events/)
- [FastAPI dependencies (`Depends`)](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [Starlette middleware (pure ASGI vs BaseHTTPMiddleware)](https://www.starlette.io/middleware/)
- [structlog contextvars](https://www.structlog.org/en/stable/contextvars.html)
- [prometheus_client](https://github.com/prometheus/client_python)
- [authlib JOSE / JWT docs](https://docs.authlib.org/en/latest/jose/jwt.html)
- [hvac JWT/OIDC auth](https://python-hvac.org/en/stable/usage/auth_methods/jwt-oidc.html)
- [Vault `/sys/health` HTTP API](https://developer.hashicorp.com/vault/api-docs/system/health)
- [respx mock router](https://lundberg.github.io/respx/)
- [OIDC core — token validation](https://openid.net/specs/openid-connect-core-1_0.html#TokenResponseValidation)
- [uv project structure](https://docs.astral.sh/uv/concepts/projects/)
- [uv production Docker pattern](https://docs.astral.sh/uv/guides/integration/docker/)

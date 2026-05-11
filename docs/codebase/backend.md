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

* Persistence layer (Task #27) — `src/meho_backplane/db/` houses the
  SQLAlchemy 2.x async engine (`engine.py`), the per-request
  session-factory dependency (`get_session`), and the
  DB-migration-state readiness probe (`migrations.py`). The probe
  compares the database's current Alembic revision (read from
  `alembic_version` via `MigrationContext.configure(...).get_current_revision()`)
  against the head defined by `ScriptDirectory.from_config(cfg).get_current_head()`;
  three failure modes (DB unreachable, table missing, revision
  diverged) collapse onto a single `ProbeResult` with `ok=False` and
  a structured `detail` string (`check_failed: <ExcClass>` /
  `current=<sha> head=<sha>` / `no_migrations: …`). The probe is
  registered as **async** against the readiness registry, which now
  accepts both sync and async probes via `register_probe` and is
  evaluated by `run_probes_async()` from `/ready`. The lifespan
  hook also **eagerly** instantiates the SQLAlchemy async engine
  (`get_engine()` is called before `yield`) so the pool is built and
  `DATABASE_URL` is validated at process boot rather than on the
  first incoming request — without the pre-warm the very first
  `/ready` poll would absorb the engine-construction cost. The
  shutdown path `await`s `dispose_engine()` so the asyncpg pool
  closes cleanly. `/api/v1/health.db.migrated` is no longer
  hardcoded `null`: it carries the probe's `ok` value (G2.2-T3's
  forward-looking placeholder is now wired through).
* Alembic configuration (Task #27) — `backend/alembic.ini` plus the
  async-aware `backend/alembic/env.py` follow the upstream Alembic
  cookbook's async pattern: `async_engine_from_config` + a sync
  inner `do_run_migrations` invoked through `run_sync`. The URL is
  not pinned in `alembic.ini`; `env.py` resolves it from the same
  `DATABASE_URL` env var the running backplane reads, so the
  migration runner and the request hot path can never drift. T28
  populates `target_metadata` with `meho_backplane.db.models.Base`
  so `alembic revision --autogenerate` diffs the model graph
  against the live schema. `alembic.ini` and the `alembic/` tree are
  also shipped as package data under `meho_backplane/` (via the
  `[tool.hatch.build.targets.wheel.force-include]` table in
  `backend/pyproject.toml`) so installed-wheel deployments resolve
  them via `importlib.resources.files('meho_backplane')` rather than
  needing the source tree on disk. The `find_alembic_ini` resolver
  walks four locations in order: `$ALEMBIC_CONFIG` env-var override
  (ops escape hatch), `importlib.resources` package data (wheel
  layout), the current working directory (matches the `alembic` CLI's
  rule), and the source-tree layout (`__file__.parents[3]` for the
  editable-install dev case).
* Audit-log persistence (Task #28) — `backend/alembic/versions/0001_create_audit_log.py`
  is the first revision on the schema, creating the `audit_log` table
  plus two b-tree indexes (`audit_log_occurred_at_idx`,
  `audit_log_operator_sub_idx`). The migration is dialect-aware: PG
  gets `gen_random_uuid()` / `now()` / `'{}'::jsonb` server defaults;
  SQLite (the dev/test driver via aiosqlite) skips them and lets the
  ORM Python-side defaults fire. Column types use SQLAlchemy's
  portable `Uuid` and `JSON().with_variant(JSONB(), "postgresql")`
  so the same migration runs cleanly on both dialects without
  branching the column shape itself. The
  `meho_backplane.db.models.AuditLog` ORM model carries the same
  field set with `Mapped[...]` typed-mapped annotations.
* Migration runner + CI guard (Task #29) — `backend/src/meho_backplane/db/migrate.py`
  is the one-file CLI entrypoint the Helm pre-install / pre-upgrade Job
  (G2.5-T3) runs before the Deployment rolls forward; it reuses
  `alembic_config()` from `db/migrations.py` so the migration that
  *applies* and the readiness probe that *verifies* never disagree on
  which `alembic.ini` they targeted, then calls
  `alembic.command.upgrade(cfg, "head")` and exits 0 on success or 1
  on failure (with a `migration_failed: <ExcClass>: <msg>` line on
  stderr). The Dockerfile keeps a single `CMD ["uvicorn", ...]` for
  the serve mode; the migrate mode is reached by the Helm Job
  overriding `command: ["python"]; args: ["-m", "meho_backplane.db.migrate"]`
  — no second image, no second entrypoint script. The CI guard at
  `scripts/ci/check_migration_compat.py` runs on every PR touching
  `backend/alembic/versions/**` (`.github/workflows/migration-compat.yml`)
  and rejects destructive patterns inside the `upgrade()` function:
  `op.drop_column` / `op.drop_table` / `op.rename_table`,
  `op.alter_column(..., new_column_name=...)`,
  `op.alter_column(..., nullable=False)`, and any
  `op.execute(...)` whose payload matches `DROP COLUMN` /
  `DROP TABLE` / `RENAME TABLE` / `RENAME COLUMN` /
  `ALTER ... SET NOT NULL`. Detection is a dual AST-plus-regex pass
  so f-string and variable-arg payloads can't smuggle destructive
  SQL past the AST. `downgrade()` is intentionally exempt because
  production never invokes `alembic downgrade` (rollback is image-
  revert + forward-compat schema discipline, per Goal #11 DoD bullet
  3); the first migration's `downgrade()` legitimately drops the
  table it just created and a flat scan would trip on it.
* Forward-compat regression test (Task #30) —
  `backend/tests/test_migration_rollback.py` is the unit-test-level
  proof of Goal #11 DoD bullet 3's *code-side* discipline: the
  running backplane image must tolerate a schema *ahead* of it
  (revision N+1 columns the revision-N image doesn't know about).
  The test spins up `postgres:16-alpine` via testcontainers, runs
  `alembic upgrade head` to revision N, applies a synthetic
  additive migration from
  `backend/tests/fixtures/synthetic_n_plus_1.py` (two columns —
  `future_field text` and `future_jsonb_field jsonb`, both with
  PostgreSQL-side defaults), then drives a real authenticated
  `GET /api/v1/health` request through the production app. The
  load-bearing assertion is **negative**: the synthetic columns
  hold their PG-side defaults verbatim, proving the revision-N
  ORM/middleware never wrote to columns it should not know about
  (the property a `SELECT *`-shaped query or a future-aware column
  list would falsify). The synthetic migration deliberately lives
  outside `backend/alembic/versions/` so the production migration
  graph and the CI guard's path filter stay undisturbed. The test
  skips on agent sandboxes without Docker (same heuristic as
  `tests.test_db_engine.TestPostgresIntegration`); CI runs it on
  the runner pool that provisions Docker. The cluster-level proof
  of the same property — exercising real `helm rollback` against a
  running deployment — is Goal #11's G2.8-T3, intentionally out of
  scope here.
* Audit middleware (Task #28) — `backend/src/meho_backplane/audit.py`
  defines the **pure-ASGI** `AuditMiddleware` that writes one row to
  `audit_log` per authenticated request **before** the response yields
  back to the ASGI send chain. Pure-ASGI is required (not
  `starlette.middleware.base.BaseHTTPMiddleware`): `BaseHTTPMiddleware`
  runs the wrapped app inside an `anyio.create_task_group` /
  `task_group.start_soon` pair, which means contextvars set inside
  the handler — including the `operator_sub` bound by
  `verify_jwt_and_bind` — disappear from the dispatch task after
  `await call_next(...)`. Empirically verified against starlette
  1.0.0; the pure-ASGI shape preserves the binding intact. The
  middleware buffers the inner app's `http.response.start` /
  `http.response.body` send messages until the audit insert
  completes; on success it forwards them verbatim, on failure it
  discards them and emits a fresh 500
  `{"detail": "audit_write_failed"}` (fail-closed contract — an
  unaudited action is an unallowed action). Skip rules: requests
  without `operator_sub` in contextvars (public surfaces, 401 paths,
  any unauthenticated request) bypass the audit write entirely.
* Middleware stack ordering (Task #28) — registration order in
  `main.py` is now load-bearing for both context propagation **and**
  audit semantics. ASGI: `client → RequestContextMiddleware →
  AuditMiddleware → router → handler`. Achieved with two
  `app.add_middleware` calls: `AuditMiddleware` first (becomes
  innermost), `RequestContextMiddleware` second (becomes outermost,
  per `add_middleware`'s last-added-is-outermost rule). The order
  ensures `request_id` is bound before audit reads it on entry, and
  `operator_sub` is bound by the handler before audit reads it on
  exit — and that the fail-closed 500 still carries
  `RequestContextMiddleware`'s `X-Request-Id` response header so the
  operator has a correlation crumb on the failure path.

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
| `lifespan` | `src/meho_backplane/main.py` | FastAPI lifespan async context manager. At startup: `configure_logging()` runs once and the Keycloak / Vault / DB readiness probes are registered. At shutdown: `await dispose_engine()` closes the SQLAlchemy async pool cleanly. |
| `ProbeFn` (alias) | `src/meho_backplane/health.py` | `Callable[[], ProbeResult] \| Callable[[], Awaitable[ProbeResult]]`. The registry accepts both sync and async probes; the `/ready` handler dispatches via `inspect.iscoroutinefunction`. |
| `ProbeResult` (`dataclass`) | `src/meho_backplane/health.py` | Frozen record `(name, ok, detail)` returned by every readiness probe. Surfaced verbatim in the `/ready` response body. |
| `register_probe` / `run_probes` / `run_probes_async` / `clear_probes` | `src/meho_backplane/health.py` | Public registry API. G2.2 (Vault, Keycloak) and G2.3 (DB migrations) call `register_probe` at startup. `run_probes()` returns the sync subset (test-friendly); `run_probes_async()` awaits async probes too and is what `/ready` calls. `clear_probes` is test-only. |
| `health.router` (`/healthz`, `/ready`) | `src/meho_backplane/health.py` | Liveness and readiness endpoints. `/healthz` is unconditional 200; `/ready` aggregates the probe registry and **fails closed on the empty default** (vacuous-truth trap explicitly guarded). |
| `version.router` (`/version`) | `src/meho_backplane/version.py` | Build identity. Reads `GIT_SHA` and `BUILD_DATE` env vars (injected via `docker build --build-arg`); falls back to `"unknown"` when unset or empty. `chart_version` is `None` until G2.5. |
| `configure_logging` | `src/meho_backplane/logging.py` | Configures structlog: `merge_contextvars` → `add_log_level` → `TimeStamper(iso, utc)` → `JSONRenderer`, writing to stdout. Idempotent. |
| `RequestContextMiddleware` | `src/meho_backplane/middleware.py` | Pure-ASGI middleware. Per request: extracts/mints a `request_id`, clears any leftover contextvars and binds the new `request_id`, mirrors it onto the `X-Request-Id` response header, increments `http_requests_total{method,path,status}`, emits one `request_completed` JSON log line with method / path / status / duration_ms (which inherits any contextvars bound during the request, including `operator_sub` from `verify_jwt_and_bind`). |
| `verify_jwt_and_bind` | `src/meho_backplane/middleware.py` | FastAPI dependency wrapper around `verify_jwt`. On successful validation, binds `operator_sub` (the JWT's `sub` claim) into structlog contextvars so every subsequent log line in the request scope carries operator identity automatically. Authenticated routes use `Depends(verify_jwt_and_bind)` instead of `Depends(verify_jwt)` directly. Lives alongside the middleware because `RequestContextMiddleware`'s request-entry `clear_contextvars` call is what guarantees the bound key does not leak across requests reusing the same asyncio task. |
| `SENSITIVE_HEADERS` | `src/meho_backplane/middleware.py` | `frozenset({b"authorization", b"cookie", b"x-api-key"})`. The middleware never logs the values of these headers; redaction is enforced by *not* logging request headers at all in v0.1, with a `tests/test_observability.py` regression test. |
| `HTTP_REQUESTS_TOTAL` | `src/meho_backplane/metrics.py` | Module-level `prometheus_client.Counter` registered against the default registry. Labels: `method`, `path`, `status`. `path` is the matched FastAPI route template when available, bounding label cardinality. |
| `render_metrics` | `src/meho_backplane/metrics.py` | Returns `(body, content_type)` for the `/metrics` route. Pins `text/plain; version=0.0.4; charset=utf-8` — the legacy Prometheus format every scraper accepts (`prometheus_client>=0.21` advertises 1.0.0 in `CONTENT_TYPE_LATEST`, but 0.0.4 stays universally compatible). |
| `Settings` / `get_settings` | `src/meho_backplane/settings.py` | Pydantic v2 model + `lru_cache`-singleton accessor for the Keycloak knobs (`KEYCLOAK_ISSUER_URL`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_JWKS_CACHE_TTL_SECONDS`, `KEYCLOAK_JWT_LEEWAY_SECONDS`), the Vault knobs (`VAULT_ADDR`, `VAULT_OIDC_ROLE`, `VAULT_OIDC_MOUNT_PATH`, `VAULT_NAMESPACE`, `VAULT_TIMEOUT_SECONDS`), and the database knobs (`DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_POOL_TIMEOUT`). `DATABASE_URL` is required and validated by a Pydantic `@field_validator` that rejects sync DSNs (`postgresql://`, `sqlite:///`, `postgresql+psycopg2://`) — only `postgresql+asyncpg://` and `sqlite+aiosqlite://` are accepted, matching ADR 0004's async-only mandate. Tests reset via `get_settings.cache_clear()`. |
| `create_engine_for_url` / `get_engine` / `get_sessionmaker` / `get_session` / `dispose_engine` | `src/meho_backplane/db/engine.py` | SQLAlchemy 2.x async engine + per-request session factory (Task #27). `get_engine` is lazy + cached; `get_session` is the FastAPI `Depends` that yields a transaction-bracketed `AsyncSession`. SQLite URLs (dev / aiosqlite) prune the `pool_size` / `pool_timeout` kwargs because StaticPool rejects them; Postgres URLs (asyncpg) keep them. `dispose_engine` is awaited from the lifespan shutdown so asyncpg's pool releases its connections cleanly. |
| `db_migration_probe` / `alembic_config` / `find_alembic_ini` | `src/meho_backplane/db/migrations.py` | Async readiness probe + Alembic config helpers (Task #27). The probe compares `MigrationContext.configure(conn).get_current_revision()` against `ScriptDirectory.from_config(cfg).get_current_head()` over an `AsyncEngine.connect()`/`run_sync` pair; every failure mode collapses onto `ProbeResult(ok=False)` with a redacted `detail` (no operator-controllable URL substrings). |
| `Base` / `AuditLog` | `src/meho_backplane/db/models.py` | SQLAlchemy 2.x `DeclarativeBase` plus the v0.1 `AuditLog` model (Task #28). Columns use portable `Uuid` and `JSON().with_variant(JSONB(), "postgresql")` types so the model and migration run cleanly on both PG (production) and SQLite (dev/test). Indexes on `occurred_at` and `operator_sub` are declared in `__table_args__`. |
| `AuditMiddleware` | `src/meho_backplane/audit.py` | Pure-ASGI middleware (Task #28). For every authenticated request (`operator_sub` present in contextvars) writes one `audit_log` row synchronously before yielding the response back to the send chain. Buffers `http.response.start`/`http.response.body` messages so the fail-closed path can replace them with a 500 `{"detail": "audit_write_failed"}` when the audit insert raises. Skips public surfaces and 401 paths by keying on the contextvar's presence rather than path-matching. |
| `0001_create_audit_log` | `backend/alembic/versions/0001_create_audit_log.py` | First migration on the schema (Task #28). Creates the `audit_log` table plus `audit_log_occurred_at_idx` and `audit_log_operator_sub_idx`. PG gets `gen_random_uuid()` / `now()` / `'{}'::jsonb` server defaults; SQLite branches skip them and rely on the ORM Python-side defaults. Downgrade drops the table — the only revertible operation here because no production data exists yet; subsequent migrations land under the additive-only discipline enforced by Task #29's CI guard. |
| `meho_backplane.db.migrate.main` | `backend/src/meho_backplane/db/migrate.py` | Helm pre-install / pre-upgrade Job entrypoint (Task #29). Calls `alembic.command.upgrade(cfg, "head")` against the `alembic_config()` resolved by `db/migrations.py`. Returns 0 on success / 1 on failure with `migration_failed: <ExcClass>: <msg>` on stderr. No CLI flags by design — schema target is always `head`, and forward-only is enforced by not exposing `downgrade`. |
| `check_migration_compat` | `scripts/ci/check_migration_compat.py` | CI guard (Task #29). Scans every `backend/alembic/versions/*.py` migration's `upgrade()` function for destructive patterns via a dual AST + regex detector; exit 0 on a clean tree, 1 on any violation. Honours an optional positional argument (a versions directory) so the test suite can point the guard at synthetic fixtures without monkeypatching module state. Workflow trigger is path-filtered to `backend/alembic/versions/**` plus the script itself. |
| `backend/alembic.ini` + `backend/alembic/env.py` + `backend/alembic/script.py.mako` | repo paths | Alembic configuration (Task #27). `env.py` follows the upstream async cookbook: `async_engine_from_config` + `connection.run_sync(do_run_migrations)`. URL is sourced from `DATABASE_URL` so the migration runner and the running backplane share one knob. `versions/` ships empty; first migration lands in T28. |
| `Operator` | `src/meho_backplane/auth/operator.py` | Frozen pydantic v2 model carrying validated claims (`sub`, `name`, `email`, `raw_jwt`). Returned by `verify_jwt`; consumed by every authenticated route from G2.2-T3 onward. `raw_jwt` is preserved verbatim for G2.2-T2's Vault forward-auth. |
| `verify_jwt` | `src/meho_backplane/auth/jwt.py` | FastAPI dependency: parses `Authorization: Bearer ...`, fetches/caches Keycloak's JWKS, validates signature + `iss` + `aud` + `exp` (with leeway), refreshes JWKS on a kid miss, and returns an `Operator`. Every failure mode collapses to a terse 401. |
| `keycloak_readiness_probe` | `src/meho_backplane/auth/jwt.py` | Synchronous probe registered with the readiness registry at app lifespan startup. Hits `{issuer}/.well-known/openid-configuration` then `jwks_uri`; failure detail surfaces only the exception class name to avoid leaking issuer URLs into 503 payloads. |
| JWKS cache | `src/meho_backplane/auth/jwt.py` (`_jwks_cache`, `_jwks_fetched_at`, `_jwks_lock`) | Module-level dict + monotonic-fetched timestamp + asyncio lock. TTL-bounded (default 5 min) and kid-rotation refreshed (one forced re-fetch per request on a kid miss). Single-worker design; v0.2 may move to Redis when multi-worker uvicorn is needed. |
| `vault_client_for_operator` | `src/meho_backplane/auth/vault.py` | Async context manager: builds an `hvac.Client` from settings, performs `client.auth.jwt.jwt_login(role, jwt, path)` against the configured mount path, yields the authenticated client, and revokes the issued token on exit (best-effort). Every blocking hvac call runs through `asyncio.to_thread` because hvac is `requests`-based and FastAPI does not auto-offload sync I/O inside `async def` callables. Per-request login by design (v0.1); v0.2 may add a per-operator cache. |
| `vault_readiness_probe` | `src/meho_backplane/auth/vault.py` | Synchronous probe registered with the readiness registry at app lifespan startup. Calls `client.sys.read_health_status(method='GET')` (unauthenticated) and classifies the response — `sealed=False`/`http_429`/`http_472`/`http_473` → ok; `sealed`/`uninitialized`/connection-error → not ok. Detail strings never echo the Vault URL or namespace. |
| `VaultClientError` / `VaultUnreachableError` / `VaultRoleDeniedError` | `src/meho_backplane/auth/vault.py` | Backplane-side exception hierarchy. Callers catch `VaultClientError` for a single error response shape, or one of the subclasses to map to specific HTTP statuses. The hierarchy lets consumers avoid importing `hvac` directly. |
| `api/v1/health.router` (`/api/v1/health`) | `src/meho_backplane/api/v1/health.py` | Authenticated federation-proof endpoint (Task #24, extended in Task #27). `GET` handler runs through `Depends(verify_jwt_and_bind)`, calls `vault_client_for_operator(operator)`, reads `secret/meho/test/federation` (KV v2), invokes `db_migration_probe()` to populate `db.migrated`, and returns `HealthResponse` (operator identity + vault status + db status). Vault unreachable / role denied / read failure / DB unreachable / revision diverged all surface as structured fields on a 200 response — never 5xx. |
| `HealthResponse` / `OperatorIdentity` / `VaultStatus` / `DbStatus` | `src/meho_backplane/api/v1/health.py` | Frozen pydantic v2 response models. `OperatorIdentity` deliberately excludes `raw_jwt` so the bearer token never appears in the response body. `DbStatus.migrated` reflects the T27 DB-migration-state probe verdict (true when current matches Alembic head, false otherwise; `bool \| None` is preserved for forward compatibility with chassis-stage decoders). `VaultStatus.detail` carries only structured tokens (`version=N`, `read_failed: <ExcClass>`, `login_failed: <ExcClass>`) — no operator-controllable URL substrings. |
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
   - `GET /ready` → calls `run_probes_async()` (which awaits async
     probes and calls sync probes inline) and translates the
     aggregate into a 200 / 503 `JSONResponse`.
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
| `sqlalchemy[asyncio]` | ≥ 2.0 | Async ORM + Core (per ADR 0004). The `asyncio` extra pulls `greenlet`, which SQLAlchemy 2.x async needs to bridge sync ORM callsites onto the event loop. |
| `asyncpg` | ≥ 0.29 | Async PostgreSQL driver (per ADR 0004). Faster than `psycopg`'s sync wrapper for the read/write patterns of an audit log + per-operator metadata, and the only async driver SQLAlchemy 2.x officially supports. |
| `alembic` | ≥ 1.13 | Schema migrations. The async-aware `env.py` follows the upstream cookbook pattern (`async_engine_from_config` + `connection.run_sync(do_run_migrations)`); Alembic itself stays sync, but reaches into asyncpg via the engine. |
| (dev) `aiosqlite` | ≥ 0.19 | Async SQLite driver used for local-dev / test DBs that do not need Docker. The probe + engine module both work against `sqlite+aiosqlite://` URLs because the driver-specific surface is encapsulated by SQLAlchemy. |
| (dev) `testcontainers` | ≥ 4.0 | Spins up `postgres:16-alpine` for the end-to-end test in `tests/test_db_engine.py::TestPostgresIntegration`. Skipped gracefully when the Docker socket is absent — the SQLite-async coverage stays always-on. |
| (dev) `pytest` ≥ 8 | | Test runner. |
| (dev) `pytest-asyncio` ≥ 0.23 | | Async test support; `asyncio_mode = "auto"` in pyproject. |
| (dev) `cryptography` ≥ 42.0 | | RSA keypair generation in test fixtures (authlib pulls it transitively in production). |
| (dev) `respx` ≥ 0.21 | | httpx-native mock router used to stub Keycloak's discovery + JWKS endpoints in `tests/test_auth_jwt.py`. |
| (dev) `pytest-cov` ≥ 5.0 | | Line-coverage reporting; Task #25 acceptance criterion pins `auth/jwt.py` and `auth/vault.py` at >90% line coverage. |
| (dev) `ruff` ≥ 0.5 | | Lint + format. |
| (dev) `mypy` ≥ 1.10 | | Strict type checking. |

## Container image (multi-arch build)

The backplane ships as a multi-stage Docker image at `backend/Dockerfile`,
built for `linux/amd64` (Hetzner deploy target) and `linux/arm64`
(Apple Silicon developer machines, GitHub Actions arm64 runners).

### Base image digest pin (Task #32)

`ARG PYTHON_BASE_DIGEST` near the top of the Dockerfile pins the base
image to a specific OCI manifest-list digest, not the floating
`python:3.12-slim` tag. The digest references the manifest list, not a
per-arch image — buildx resolves it to the correct `linux/amd64` or
`linux/arm64` child at build time, so one pin covers both architectures.

| Field | Value |
| --- | --- |
| Base | `docker.io/library/python:3.12-slim` |
| Pinned digest | `sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe` |
| Pinned on | 2026-05-10 |
| Verify | `docker manifest inspect python:3.12-slim` or the [Docker Hub `tags` API](https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim) |

The uv installer image (`ghcr.io/astral-sh/uv`) is also digest-pinned
at `0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7`
in the same file. The two pins move together when the toolchain
upgrades.

**Refresh policy.** Every digest bump lands in a dedicated PR titled
`chore(backend): bump python:3.12-slim base digest to <new>`. Open a
new PR rather than batching the digest bump into an unrelated change
— the supply-chain audit trail (G2.4-T3 cosign + G2.4-T4 SBOM) reads
this PR as the provenance event for the upgrade. The same rule
applies to the uv digest.

### arm64 cross-compile cost (Task #32)

The Dockerfile does *not* split the builder stage onto
`--platform=$BUILDPLATFORM` (the cross-compile pattern from the Docker
multi-platform guide). Buildx runs the full Dockerfile once per target
platform; for the non-native architecture this means QEMU user-mode
emulation translates every guest instruction.

**Expect arm64 builds on an amd64 host to take 3–5× longer than the
native amd64 build.** The dominant cost is `uv sync --no-editable`
invoking the wheel installer for compiled extensions:

- `asyncpg` — C extension, native build under QEMU
- `cryptography` — Rust + C extensions, slowest single dep
- `pydantic-core` — Rust extension
- `greenlet` — C extension

Cross-compile via `--platform=$BUILDPLATFORM` was considered and
rejected: it works cleanly for pure-Python projects but breaks here
because the builder would be installing wheels for the wrong arch
(or recompiling Rust crates without a cross toolchain configured).
The CI pipeline (G2.4-T2) runs amd64 and arm64 in parallel jobs so
wall-clock time is bounded by the slower job, not the sum; the
self-hosted `meho-runners` pool (introduced in PR #160) can provision
arm64 nodes natively when the team is ready to skip QEMU entirely
for the arm64 leg.

### .dockerignore discipline

`backend/.dockerignore` excludes:

- `tests/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`,
  `.ruff_cache/` — never shipped in the runtime image
- `.git/`, `.github/` — defence in depth against build contexts
  rooted above `backend/`
- `*.md` (whitelist-exempt: `README.md` for hatchling
  `[project].readme`)
- `Dockerfile*`, `.dockerignore`, `docker-compose*.yml` — never
  needed inside the image
- `.env*`, `*.pem`, `*.key`, `*.crt` — secret-shaped paths,
  whitelist-exempt: `.env.example`

`README.md` is intentionally **not** excluded — hatchling errors at
wheel-build time with `OSError: Readme file does not exist:
README.md` when `[project].readme` is set and the file is absent.

### OCI image labels

The runtime stage stamps the published image with OCI annotations
(see [image-spec annotations](https://github.com/opencontainers/image-spec/blob/main/annotations.md)):

- `org.opencontainers.image.source=https://github.com/evoila/meho`
- `org.opencontainers.image.licenses=Apache-2.0`
- `org.opencontainers.image.title=meho-backplane`
- `org.opencontainers.image.description=…`
- `org.opencontainers.image.revision=${GIT_SHA}` (filled by CI via
  `--build-arg GIT_SHA=$(git rev-parse HEAD)`)
- `org.opencontainers.image.created=${BUILD_DATE}` (RFC 3339 UTC)
- `org.opencontainers.image.vendor=evoila`

`revision` and `created` flow into `GET /version` via the same build
args, so the registry view and the running app agree on identity.

### Cosign keyless signing (Task #34, ADR 0006)

Every image push from `.github/workflows/image.yml` is signed with
[cosign](https://github.com/sigstore/cosign) keyless OIDC against the
public Sigstore trust root (Fulcio CA + Rekor transparency log). There
are no private signing keys to custody, rotate, or distribute — the
trust anchor is the **certificate identity** baked into the Fulcio
certificate at signing time:

| Field | Value |
| --- | --- |
| OIDC issuer | `https://token.actions.githubusercontent.com` |
| Identity (main pushes) | `https://github.com/evoila/meho/.github/workflows/image.yml@refs/heads/main` |
| Identity (v* tag pushes) | `https://github.com/evoila/meho/.github/workflows/image.yml@refs/tags/v<x.y.z>` |
| Transparency log | public Rekor (`rekor.sigstore.dev`) |
| Cosign version | v3.0.6 (pinned via `sigstore/cosign-installer@v4.1.2`) |

The flow on every non-PR build:

1. The job's `id-token: write` permission (granted at the workflow level
   in PR #164's forward-compat scaffold) lets the runner mint a short-
   lived GitHub Actions OIDC token whose subject encodes the repo +
   workflow file path + ref.
2. `cosign sign --yes ghcr.io/evoila/meho@<digest>` hands the OIDC
   token to Fulcio; Fulcio mints a ~10-minute x509 certificate binding
   an ephemeral ECDSA-P256 keypair to the OIDC identity.
3. cosign signs the manifest-list digest with the ephemeral key and
   uploads `{signature, certificate}` to public Rekor. The Rekor
   inclusion proof is what verifies the (now-expired) Fulcio
   certificate was valid at signing time — disabling tlog upload would
   make the signature unverifiable post-expiry and is forbidden by
   ADR 0006.

**Signing by digest, not by tag.** `docker/build-push-action@v7`'s
`outputs.digest` is the manifest-list digest, not a per-arch child.
Signing the manifest list covers every tag alias pointing at it
(`:sha-<long>`, `:main`, `:v<x.y.z>`) and every per-arch child
(linux/amd64 + linux/arm64) without separate invocations. Tags are
mutable; digests are content-addressed — the signature binds to *what*
was signed, not the human-readable name.

**The `--yes` flag** is the non-interactive confirmation prompt cosign
otherwise emits before contacting Fulcio. CI has no TTY. It is *not*
related to the deprecated `COSIGN_EXPERIMENTAL=1` of the cosign-1.x
era; cosign 2.x+ makes keyless the default for `cosign sign` and the
experimental gate is gone.

**Why a single workflow signs many tag aliases.** ADR 0006 anticipates
a future `release.yml` that bundles image + Helm chart + CLI tarball
signing under one identity. For now, the image workflow signs only
the image; the chart and CLI workflows (G2.5, G2.6) will each carry
their own cosign step under their own `<workflow>.yml@<ref>` identity.
Operator verification policies pattern-match across them
(`--certificate-identity-regexp '.../\.github/workflows/.*\.yml@.*'`)
where appropriate.

**Verification commands** live in `backend/README.md` under "Verifying
image signatures" — same regex, same issuer, copy-pasteable into an
operator runbook. The downstream
[`claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
`install.sh` runs that exact `cosign verify` as a **gating** check
before `docker pull`; a failed verification aborts the install with
the expected-identity error message.

**Action pin discipline.** `sigstore/cosign-installer` is pinned to a
commit SHA (`6f9f17788090df1f26f669e9d70d6ae9567deba6`, tag `v4.1.2`)
matching the rest of the workflow's third-party-action pinning policy.
The action installs cosign `v3.0.6` (its default) — bumping the action
major version (v3 → v4) is a deliberate Renovate / Dependabot review
because v4 dropped cosign-1.x support.

### SBOM-as-attestation + vulnerability scan (Task #35)

Every pushed image carries a syft-generated SPDX 2.x SBOM that cosign
attaches to the manifest-list digest as an [in-toto attestation](https://docs.sigstore.dev/cosign/verifying/attestation/)
signed under the same keyless identity as the image. Operators verify
the attestation with `cosign verify-attestation --type spdxjson …` and
get back the full bill of materials plus a Rekor-anchored signature
chain — same identity claim, single trust root. Verification commands
live in `backend/README.md` under "Verifying the SBOM attestation".

The flow on every non-PR build:

1. `anchore/sbom-action` (which wraps syft) pulls the image by digest
   and emits `sbom.spdx.json` next to the workflow workspace. The
   action also auto-attaches the SBOM as a workflow artefact so it
   stays downloadable independently of the attestation.
2. `cosign attest --predicate sbom.spdx.json --type spdxjson
   ghcr.io/evoila/meho@<digest>` wraps the predicate as an in-toto
   statement, signs it under the same keyless OIDC flow as the image
   signature, and uploads the attestation envelope + Fulcio
   certificate to Rekor. Attestation is bound to the manifest-list
   digest so every tag alias and per-arch child is covered by one
   call.
3. The predicate-type identifier (`spdxjson`) tells cosign to wrap
   the SBOM under the SPDX-JSON in-toto predicate type
   (`https://spdx.dev/Document`). The downstream consumer's
   `cosign verify-attestation --type spdxjson` reads the same
   identifier, decodes the base64 payload, and exposes the SPDX
   document as the attestation's `.predicate`.

The supply-chain rationale: signing the image proves *who* built it;
the SBOM attestation proves *what's in it*. Both anchor to the same
Fulcio identity (`image.yml@<ref>`), so downstream `install.sh` (the
dogfooding consumer) verifies the entire chain — image, build
provenance, bill of materials — with one trust anchor.

**Trivy report-only scan.** `aquasecurity/trivy-action` runs after the
attestation step against the same digest. The scan emits SARIF
covering OS packages + the locked Python deps inside `/app/.venv`,
filtered to `CRITICAL,HIGH` with `ignore-unfixed: true` so the
results list only actionable findings. SARIF is uploaded **twice**:

- `github/codeql-action/upload-sarif` posts the report to the GitHub
  Security tab (Code scanning alerts, category `trivy-image-scan`).
  Requires `security-events: write` at the workflow level (granted
  in the permissions block alongside `id-token: write`).
- `actions/upload-artifact` attaches the same SARIF as a 30-day
  workflow artefact (`trivy-results`) so operators without
  Security-tab access can still download the file via
  `gh run download`.

**v0.1 is report-only by deliberate split.** `exit-code: '0'` keeps
the workflow green regardless of findings. Goal #11 splits "scan
runs" from "scan gates the build" so the team can establish a
baseline noise level before committing to a remediation policy. v0.2
will flip `exit-code` to fail on a defined severity threshold once
the baseline is known and triage cadence is sustainable.

**Action pin discipline (same rule as cosign).** Every third-party
action in the new steps is SHA-pinned with the human-readable tag in
a trailing comment: `anchore/sbom-action@…` (v0.24.0),
`aquasecurity/trivy-action@…` (v0.36.0),
`github/codeql-action/upload-sarif@…` (v4.35.4),
`actions/upload-artifact@…` (v7.0.1). Renovate / Dependabot bumps
these on the same review cadence as `sigstore/cosign-installer`.

### Cross-repo deploy trigger (Task #51)

After image push, sign, attest, and scan, `image.yml`'s final step
fires a `repository_dispatch` event of type `meho-image-pushed` at
[`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
— the dogfooding consumer that operates MEHO against the rke2-infra
lab cluster. The dispatch is the upstream half of the cross-repo
handshake; the consumer-side listener (`.github/workflows/meho-deploy.yml`
on the consumer) is consumer-owned per Goal #11's cross-repo deps.

The full event-shape spec lives in
[`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md);
that doc is the canonical source of truth for the payload — keep it
and `image.yml` in lock-step.

| Aspect | Value | Rationale |
| --- | --- | --- |
| `event_type` | `meho-image-pushed` | Single event type; consumer matches on `types: [meho-image-pushed]` in its workflow's `on:` block |
| `client_payload.image` | `ghcr.io/evoila/meho` | Static; the consumer reads `image@digest` to pull |
| `client_payload.digest` | `sha256:<64-hex>` from `docker/build-push-action` output | Immutable handle; the consumer deploys by digest, never by tag |
| `client_payload.tag` | `${{ steps.meta.outputs.tags }}` (newline-joined list from metadata-action) | Human-readable cross-reference; not used for pulls |
| `client_payload.commit` | `${{ github.sha }}` (full 40-char SHA) | Surfaces in the consumer's run logs so operators can trace which evoila/meho commit produced the deploy |
| `client_payload.ref` | `${{ github.ref }}` (always `refs/heads/main` here) | Confirms the trigger origin; the consumer can branch on tag vs branch pushes if needed |

**Trigger gating.** Three conjuncts on the step's `if:`:

1. `github.event_name == 'push'` — never on PRs. PRs build the
   image but don't push it, so there's no new artefact to advertise.
2. `github.ref == 'refs/heads/main'` — main only. Tag pushes (v*
   releases) are out of scope for v0.1; the dogfooding consumer
   tracks main, not tagged releases. The cross-repo doc locks
   `ref` to `refs/heads/main`.
3. `env.RDC_DISPATCH_TOKEN != ''` — skip when the secret is missing.
   Secrets cannot be referenced inside `if:` directly per GitHub
   Actions docs; the workflow exposes the PAT via a job-level
   `env:` block (`RDC_DISPATCH_TOKEN`) so the step can test
   `env.RDC_DISPATCH_TOKEN != ''`. A rotated or revoked PAT
   degrades the workflow to "image still gets built, signed,
   attested, and pushed, just no downstream advertisement" rather
   than failing the whole run.

**Cross-repo auth: PAT, not GITHUB_TOKEN.** The default
`GITHUB_TOKEN` is scoped to the originating repo only and returns
404 against `/repos/evoila-bosnia/claude-rdc-hetzner-dc/dispatches`.
A maintainer-provisioned fine-grained PAT lives in `evoila/meho`
secrets as `RDC_DISPATCH_TOKEN`, scoped to:

- Target repository: `evoila-bosnia/claude-rdc-hetzner-dc` (single repo)
- Permissions: `metadata: read` + `actions: write` (the minimum
  surface for `POST /repos/{owner}/{repo}/dispatches`)

**One-time maintainer setup.** The PAT is not workflow-creatable; a
maintainer with org-admin access on `evoila-bosnia/claude-rdc-hetzner-dc`
mints it, then stores it as the `RDC_DISPATCH_TOKEN` secret on
`evoila/meho` (Settings → Secrets and variables → Actions). When
the PAT is missing the step skips silently; when the PAT is present
but invalid (revoked, wrong scope) the step fails-loud — a 401/403
surfaces in the workflow log so the operator knows to rotate.

**v0.2 improvement on the horizon.** The PAT is the v0.1 expedient.
A GitHub App + installation token would carry shorter-lived
credentials (1-hour installation tokens vs PATs that live until
manually rotated), and CodeRabbit consistently flags long-lived PATs
as such. The GitHub App migration is tracked separately; v0.1 ships
with the PAT because the rotation discipline is captured on the
coordination tracker
([`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md))
and the consumer-side acceptance bullet covers it.

**Failure semantics.** No `continue-on-error` on the dispatch
step. A silent dispatch failure means the consumer never learns
about the new image — the lab would deploy stale revisions
indefinitely. The fail-loud posture turns rotation issues into
visible alerts (red workflow run) instead of silent drift. Per
issue #51 AC #5 the *missing-secret* path skips; the
*invalid-secret* path fails — the two are intentionally distinct.

## Known issues

`/ready` returns 503 until every registered probe passes. After Task
#27 the lifespan hook registers three probes: Keycloak, Vault, and the
DB-migration-state probe. A running app needs Keycloak's JWKS endpoint
reachable, Vault's `/sys/health` reachable + unsealed, **and** the
PostgreSQL database reachable with an `alembic_version` row matching
the on-disk Alembic head. T28 lands the first migration (`0001`); a
deployment that has not yet run `alembic upgrade head` will see the
probe report `ok=False` with `current=None head=0001` until the
migration runner (T29) catches up. Helm charts pointing their
kubernetes readiness probe at `/ready` before all three dependencies
are provisioned will see pods stay `NotReady` — by design.

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
- Task #27 — PG connection pool + Alembic wiring + DB-migration-state readiness probe
- Task #28 — Audit table schema + synchronous audit-write middleware
- Task #29 — Migration runner entrypoint + CI guard rejecting destructive migration patterns
- Task #32 — Multi-stage Dockerfile finalized + multi-arch buildx (linux/amd64 + linux/arm64); base image digest-pinned
- Task #33 — GHCR image push workflow (`.github/workflows/image.yml`)
- Task #34 — Cosign keyless signing of pushed images via GitHub Actions OIDC (per ADR 0006)
- Task #51 — `repository_dispatch` to claude-rdc-hetzner-dc on main image push (`meho-image-pushed` event)
- [OCI image-spec annotations](https://github.com/opencontainers/image-spec/blob/main/annotations.md)
- [Sigstore cosign keyless signing overview](https://docs.sigstore.dev/cosign/signing/overview/)
- [Sigstore CI quickstart (GitHub Actions OIDC)](https://docs.sigstore.dev/quickstart/quickstart-ci/)
- [`sigstore/cosign-installer`](https://github.com/sigstore/cosign-installer) action
- [Docker buildx multi-platform builds](https://docs.docker.com/build/building/multi-platform/)
- [SQLAlchemy 2.x async overview](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [SQLAlchemy 2.x pool / pre-ping disconnect handling](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic)
- [Alembic async migrations cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic)
- [asyncpg driver](https://magicstack.github.io/asyncpg/)
- [testcontainers-python](https://testcontainers-python.readthedocs.io/)
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

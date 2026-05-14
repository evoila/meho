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
  (Task #22). G0.1-T2 extended the dependency to extract two further
  required claims — `tenant_id` (UUID) and `tenant_role`
  (`tenant_admin` / `operator` / `read_only` `StrEnum`) — from the
  validated JWT under settings-controlled claim names
  (`JWT_TENANT_CLAIM_NAME` / `JWT_TENANT_ROLE_CLAIM_NAME`, defaults
  `tenant_id` / `tenant_role`). Each failure mode (missing claim,
  malformed UUID, unknown role) emits a distinct structlog event
  (`missing_tenant_claim` / `missing_tenant_role_claim` /
  `malformed_tenant_claim` / `unknown_tenant_role`) and surfaces as a
  401 with a matching `detail` token, so an operator chasing a
  Keycloak protocol-mapper bug can grep the JSON log line directly.
  No new protected routes are added in this task; consumers continue
  to land in G2.2-T3 (`/api/v1/health`). The auth contract itself
  does change: `verify_jwt` now requires `tenant_id` + `tenant_role`
  on every accepted JWT, so the issuer (Keycloak realm provisioning,
  G0.1-T5) must emit them — JWTs without these claims now fail 401
  with one of the four detail tokens above.
* RBAC primitive (Task #234, G0.1-T4) — `auth/rbac.py` ships
  `require_role(min_role: TenantRole)`, a function factory that
  returns a FastAPI dependency. The dependency runs after
  `verify_jwt_and_bind` and rejects operators below the requested
  minimum with HTTP 403 `insufficient_role` plus a structured
  `insufficient_role` log line carrying `operator_sub`, `actual_role`,
  and `required_role`. Role ranking is **explicit** in a private
  `_ROLE_ORDER` tuple (`read_only` < `operator` < `tenant_admin`),
  not implicit in the StrEnum, so a future enum reorder cannot
  silently invert the comparison; the minimum-role rank is resolved
  at factory call time so a typo or an enum widening that misses
  the ordering tuple surfaces as an import-time `ValueError`. Two
  stub routes (`/api/v1/rbac-test/admin`, `/api/v1/rbac-test/operator`)
  in `api/v1/rbac_test.py` exercise the dependency end-to-end; they
  are mounted only when `MEHO_ENABLE_RBAC_TEST_ROUTE=1`
  (`Settings.enable_rbac_test_route`, default `False`) so production
  deploys never expose them. CI flips the flag for the RBAC
  integration job; production keeps the routes genuinely 404. The
  primitive is what downstream Goals (G3 / G4 / G5 / G7 / G8 / G9)
  apply to write/admin handlers as they land — T4 only ships the
  primitive plus the verification surface.
* Vault forward-auth — `VaultConnector` (`connectors/vault/connector.py`,
  Task #244 G0.2-T5, refactored under G0.6-T-Refactor-Vault #390) is
  the canonical abstraction for the Vault integration. It wraps
  `vault_client_for_operator` from `auth/vault.py` and exposes
  `fingerprint`, `probe`, and `execute` per the `Connector` ABC.
  Post-G0.6 the connector registers via `register_connector_v2(...)`
  under `(product="vault", version="1.x", impl_id="vault")` rather
  than the v1 single-product entry point; per-op handlers
  (`vault_kv_read` in `connectors/vault/ops.py`) land as
  `endpoint_descriptor` rows via `register_vault_typed_operations()`
  at lifespan startup. The Vault readiness probe registered in
  lifespan delegates to `VaultConnector.probe()` and flips `/ready`
  red when `/sys/health` is unreachable, sealed, or uninitialized.
* Federation-proof endpoint — `GET /api/v1/health` (auth-required) is
  the load-bearing integration point where the entire JWT → Vault
  chain runs on every call (Task #24, refactored to dispatch under
  G0.6-T-Refactor-Vault #390). The route uses the
  `verify_jwt_and_bind` dependency wrapper, which delegates to
  `verify_jwt` and — on success — binds `operator_sub` into structlog
  contextvars. The handler dispatches `vault.kv.read` via
  `dispatch(operator=..., connector_id="vault-1.x", op_id="vault.kv.read", ...)`
  (G0.6-T5 #396), reads the test secret at
  `secret/meho/test/federation` (KV v2), and returns a structured
  JSON document with operator identity, Vault status, and a DB
  migration placeholder. The handler raises
  `VaultClientError` subclasses on login-side failure and
  read-side exceptions on read-side failure; the dispatcher's
  `connector_error` branch records the exception class name in
  `extras["exception_class"]`, and the route string-matches against
  the known VaultClientError subclass set to render
  `login_failed: <Cls>` vs `read_failed: <Cls>`. All Vault
  failure modes surface as structured fields on a 200 response —
  the smoke test never sees a 5xx from this endpoint.
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

* Broadcast substrate (G6.1-T1 #307) — `src/meho_backplane/broadcast/`
  houses the async Valkey client (`client.py`) and the
  `broadcast_readiness_probe` (`probe.py`). The lifespan hook
  registers the probe under the `"broadcast"` name and eagerly
  constructs the client so a malformed `BROADCAST_REDIS_URL` fails
  startup rather than the first `/ready` poll; redis-py is lazy about
  TCP so no socket opens until the probe issues its first `PING`.
  `Settings.broadcast_redis_url` accepts only the
  redis-py-supported schemes (`redis://`, `rediss://`, `unix://`) —
  Valkey is wire-compatible under `redis://`, and a `valkey://` typo
  would otherwise crash on first command. The probe distinguishes
  four observable outcomes in `ProbeResult.detail` (`reachable`,
  `timeout`, `unreachable: <ExcClass>`, `redis_error: <ExcClass>`)
  plus a `check_failed: <ExcClass>` safety net for unexpected
  failures, and never echoes the operator-supplied URL — same
  redaction contract as the Vault and DB probes. T3-T6 (#309-#312)
  build the publish-on-write hook, SSE endpoint, MCP resource, and the
  `meho status --watch` CLI on top of this foundation. T3 (publish-on-
  write) is now landed; T4-T6 (#310-#312) are the remaining subscribers.
* Broadcast event schema + PII classifier (G6.1-T2 #308) —
  `src/meho_backplane/broadcast/events.py` ships the `BroadcastEvent`
  Pydantic model + `classify_op` / `redact_payload` helpers. Every
  audited op produces exactly one `BroadcastEvent` at T3 publish time;
  the model is frozen so downstream consumers (T4 SSE, T6 MCP resource)
  can't mutate events mid-pipeline. The classifier locks decision #3's
  conservative PII defaults: `credential_read` ops (`vault.kv.read`,
  `vault.kv.list`) and `audit_query` ops (`audit.*` prefix) broadcast
  aggregate-only — the credential path / key / value and the audit
  filter / matched rows never reach the stream. Everything else
  broadcasts in full. Per-op opt-in to flip a sensitive class to full
  detail is a G6.3 surface; T2 ships the conservative default.
* Broadcast publish-on-write hook (G6.1-T3 #309) —
  `src/meho_backplane/broadcast/publisher.py` ships `publish_event`,
  which `XADD`s one `BroadcastEvent` per audited op onto
  `meho:feed:{tenant_id}` with `MAXLEN ~ 10000` (best-effort trim).
  Both audit paths fire the hook: `AuditMiddleware.__call__` (HTTP
  routes) and the MCP `tools/call` / `resources/read` finally blocks
  each pre-generate the audit row's UUID, pass it into the audit
  writer, and reference it on the published event so subscribers can
  JOIN broadcast events back to the canonical audit row. Fail-open by
  contract: a publish failure logs `broadcast_publish_failed`,
  increments `broadcast_publish_errors_total`, and returns silently —
  the broadcast feed is the real-time view, the audit row is the
  record-of-truth, and Valkey unreachability never propagates as a
  request-path 5xx. Subscribers reading the stream get at-most-once
  delivery; T5 CLI `--watch` (#311) and T6 MCP resource (#312) build
  on T4's SSE surface.
* Broadcast SSE feed endpoint (G6.1-T4 #310) —
  `src/meho_backplane/api/v1/feed.py` exposes
  `GET /api/v1/feed` as a `text/event-stream` Server-Sent Events
  endpoint. Subscribers issue `XREAD BLOCK 30000` against
  `meho:feed:{operator.tenant_id}` (stream key derived from the
  validated JWT — cross-tenant subscription is impossible by
  construction), filter events post-stream by optional `op_class` /
  `principal` / `target` query parameters, and emit SSE frames
  `event: broadcast\ndata: <json>\nid: <entry-id>\n\n`. Replay is
  driven by the SSE-standard `Last-Event-Id` header (preferred) or
  the explicit `since` query parameter; default cursor is Valkey's
  `$` anchor ("live-tail from now"). The generator emits a comment
  heartbeat `: heartbeat\n\n` every 30s of inactivity to keep
  intermediaries from idle-timing-out the connection. RBAC requires
  `operator` role minimum; `read_only` operators get 403. Client
  disconnect surfaces as `asyncio.CancelledError` and is swallowed
  so the audit row at session end records a clean 200 close.
  Subscribers (T5 CLI watch #311, T6 MCP resource #312, future
  Slack mirror G6.2) consume the same SSE wire shape.
* Connector dispatch (G0.2-T6 #245) —
  `src/meho_backplane/api/v1/connectors.py` exposes
  `POST /api/v1/connectors/{product}/{op_id}`. The route resolves
  `product` against the connector registry, builds a pre-G0.3 target
  stub from the operator JWT, and delegates to
  `connector.execute(target, op_id, params)`. HTTP contract: 404 for
  unknown product, 400 for unknown op-id (with `known_ops` list), 401
  for missing/expired JWT, 200 for all other outcomes including
  connector-side errors (callers inspect `OperationResult.status`).
  The `_TargetStub` placeholder is replaced with a real
  `resolve_target` call once G0.3 (#224) lands.
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
  The test spins up `pgvector/pgvector:pg16` via testcontainers, runs
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

* MCP Streamable HTTP transport entrypoint (Task #246, G0.5-T1) —
  `backend/src/meho_backplane/mcp/` is the new module hosting MEHO's
  Model Context Protocol server front. `mcp/schemas.py` defines the
  JSON-RPC 2.0 envelopes (`JsonRpcRequest`, `JsonRpcResponse`,
  `JsonRpcError`) plus the MCP 2025-06-18 lifecycle payloads
  (`InitializeRequest`, `InitializeResponse`, `ServerCapabilities`) as
  frozen Pydantic v2 models; `mcp/server.py` mounts the `APIRouter` at
  `POST /mcp`, exposes the module-level `register_method(name, handler)`
  dispatch-table API (mirroring `register_probe`'s shape), and ships
  three built-in handlers — `initialize`, `notifications/initialized`,
  and `ping`. The route follows the Streamable HTTP transport's
  single-response shape (no SSE in v0.2): requests get HTTP 200 with a
  single JSON-RPC envelope; notifications get HTTP 202 Accepted with
  no body per spec §"Sending Messages to the Server". JSON-RPC-level
  errors (parse, invalid request, method-not-found, invalid params,
  internal error) are encoded as 200 envelopes with the `error` member
  populated; transport-level failures flip the HTTP code (an
  unsupported `MCP-Protocol-Version` header → 400; missing or
  invalid Bearer → 401 — see Task #247 below). T3 (#248) layers the
  tool + resource registries (`register_mcp_tool`,
  `register_mcp_resource`); T4 (#249) populates them with the
  reference `meho.status` tool and `meho://tenant/<id>/info` resource;
  T5 (#250) replaces the chassis AuditMiddleware row (which now fires
  on every authenticated `/mcp` call thanks to T2 binding
  `operator_sub`) with a fail-closed MCP-specific audit path on
  `tools/call` + `resources/read`.

* MCP per-operation audit (Task #250, G0.5-T5) — every `tools/call` and
  `resources/read` invocation writes exactly one `audit_log` row via
  `backend/src/meho_backplane/mcp/audit.py::write_mcp_audit_row`. The
  chassis `AuditMiddleware` (`backend/src/meho_backplane/audit.py`) is
  taught to skip `/mcp` paths via the `_AUDIT_SKIP_PATH_PREFIXES`
  tuple — otherwise the JSON-RPC envelope would attribute the entire
  POST to a single row regardless of how many operations live inside,
  which is the wrong granularity for G8's audit queries. Each MCP
  handler wraps its body in a `try/finally` that derives a
  `status_code` (200 / 400 / 403 / 404 / 500) from the JSON-RPC
  outcome, packs a `payload` (`{op_id, params_hash, op_class}` for
  tools; `{uri, op_class: "read"}` for resources), and commits the
  row before propagating the result or exception. `params_hash` is
  the SHA-256 hex digest of the canonical JSON arguments
  (`sort_keys=True`, `separators=(",", ":")`) so G8 can answer "find
  all calls with these arguments" without persisting the arguments
  themselves — important for tools whose `arguments` reference secret
  paths (e.g. a future `vault.kv.read`). Fail-closed: an audit-write
  failure invalidates the operation; the MCP client sees JSON-RPC
  `-32603` Internal Error and the row's absence is the operator's
  signal to investigate the audit layer specifically.

* MCP reference tool + resource (Task #249, G0.5-T4) — the two
  reference implementations downstream G3-G9 connector tools and
  resources copy. `backend/src/meho_backplane/mcp/tools/meho_status.py`
  registers `meho.status` — a no-arg tool whose handler calls
  `meho_backplane.api.v1.health.build_health_response()` (the same
  helper the chassis `GET /api/v1/health` route uses post-T4) so the
  MCP transport returns wire-identical operator-identity + Vault +
  DB-migration data. The tool's `inputSchema` uses
  `additionalProperties: false` to reject extra arguments, and its
  `description` field is written to AI-engineering best-practice
  standards (precise about what / when / no-args). The companion
  `backend/src/meho_backplane/mcp/resources/tenant_info.py` registers
  `meho://tenant/{tenant_id}/info` as a `ResourceTemplateDefinition`
  whose handler binds `tenant_id` from the URI, validates it as a UUID,
  enforces tenant-boundary by checking the bound value equals
  `operator.tenant_id`, then queries the `tenant` table via
  `get_sessionmaker()` and returns `{id, slug, name, operator_role}`.
  Cross-tenant reads / invalid UUIDs / missing rows all surface as
  `McpInvalidParamsError` (-32602) — the JSON-RPC transport carries
  error codes, not HTTP statuses, so every input-validation failure at
  this layer maps to INVALID_PARAMS. The tenant-boundary check runs
  *before* the DB query so a probe attempt against an arbitrary UUID
  cannot learn whether that tenant exists. `build_health_response()`
  was extracted from `authenticated_health()` in `api/v1/health.py`
  during T4 so the chassis route and the MCP tool share the same
  federation-proof probe chain rather than diverging.

* MCP tenant activity feed resource (G6.1-T6a, #312) —
  `backend/src/meho_backplane/mcp/resources/tenant_feed.py` registers
  `meho://tenant/{tenant_id}/feed` as a `ResourceTemplateDefinition`
  (required role: `operator`). The handler validates the URI-bound
  `tenant_id` against `operator.tenant_id` *before* any Valkey call
  (same boundary discipline as `tenant_info`), then issues
  `XREVRANGE meho:feed:{tenant_id} + - COUNT 50` and reverses the
  result into chronological (oldest-first) order. Response shape:
  `{tenant_id, count, events: [<BroadcastEvent.model_dump(mode="json")>]}`.
  The MCP server advertises no `subscribe` capability (per the
  2025-06-18 spec, an omitted field declares no subscription
  support — the correct shape for a poll-only resource); clients
  needing live push use `GET /api/v1/feed` (SSE, T4) or
  `meho status --watch` (T5). Entries with an unknown field shape
  or whose `event` JSON doesn't validate as a `BroadcastEvent` are
  logged and skipped — same forward-compat safety net the SSE
  generator at `api/v1/feed.py` uses against a future Slack-mirror
  / downstream tool writing alternate shapes onto the same stream
  key. T6 originally bundled a load-test acceptance + Valkey-restart
  chaos test on the same task; the `/auto-implement-issue` Phase 4
  pushback split T6 into T6a (this resource + onboarding doc) and a
  follow-up T7 that lands once chart-CI hardening (#347 follow-up)
  makes the helm-test job gating.

* Broadcast load harness (G6.1-T7 shape #1, #386) —
  `backend/tests/integration/test_broadcast_load.py` drives the
  publish→SSE→MCP seam at 50 RPS for 30 s across two tenants (1500
  events total) and asserts: every published event reaches the SSE
  consumer; the tenant boundary holds throughout; the publish-errors
  Prometheus counter has zero delta; p99 publish→SSE-receive latency
  is under 5 s (the AC's hard-fail threshold; the < 1 s target is
  logged informationally); each tenant's MCP resource snapshot shows
  the last 50 events tagged with that tenant. Drives `_feed_generator`
  and `_tenant_feed_handler` directly to avoid the httpx + ASGI
  cancellation race PR #353 documented. Gated by `@pytest.mark.slow`
  + `MEHO_RUN_SLOW_TESTS=1`; CI's slow lane runs it, the default
  unit suite skips. Shape #2 (chart-CI integration + Valkey-pod
  restart chaos) follows once chart-CI hardening lands.

* Broadcast chart-CI chaos test (G6.1-T9 shape #2, #433) — a
  kubectl-orchestrated step in `.github/workflows/chart.yml`'s
  `helm-test` job that verifies the helm-installed broadcast
  subchart can carry 1500 XADDs AND recovers from a forced
  Valkey-pod restart. The test runs entirely in-cluster: one Pod
  (`broadcast-load-runner`) issues 1500 `XADD` commands via
  `redis-cli` against the broadcast Service while a background
  subprocess kills the broadcast Pod mid-run; a second Pod
  (`broadcast-recovery-probe`) then asserts the AC #2 contract —
  Deployment returns to Ready within 30 s, post-restart `XADD`
  succeeds, post-restart `XLEN` returns the new entry. The chart's
  `save ""` + `appendonly no` config (per the v0.1 ephemeral-streams
  stance) means a pod restart drops 100 % of in-flight stream data;
  the test asserts **pipeline recovery**, not data preservation,
  which matches what the chart actually contracts (see the AC
  reframe note on #433). Lives in the chart-CI workflow's
  `continue-on-error: true` lane alongside the helm-test step
  until the gating-posture flip (#432 follow-up).

* MCP tool + resource registries (Task #248, G0.5-T3) — the substrate
  every G3–G9 verb registers against. `backend/src/meho_backplane/mcp/registry.py`
  exposes `register_mcp_tool(definition, handler)` /
  `register_mcp_resource(definition, handler)` (mirroring the
  `register_probe` shape from `health.py`). Two parallel registries:
  `ToolDefinition` (active — `tools/call` invokes the handler with the
  operator + validated arguments) and `ResourceTemplateDefinition`
  (passive — `resources/read` matches a concrete URI against the
  registered RFC 6570 `{var}` templates and invokes the handler with
  the bound variables). Both definitions carry `required_role` (one
  of the three `TenantRole` values); the list methods filter against
  the calling operator's role, and `tools/call` / `resources/read`
  re-check at call time. MEHO-internal fields are stripped from the wire
  shape by `to_wire()`: `ToolDefinition` strips both `required_role` and
  `op_class`; `ResourceTemplateDefinition` strips only `required_role`
  (the resource-template model has no `op_class` field — it's a
  tool-only audit-classification hint T5 will consume).
  `backend/src/meho_backplane/mcp/handlers.py` wires five JSON-RPC
  methods to the registries: `tools/list`, `tools/call`,
  `resources/list`, `resources/templates/list`, `resources/read`.
  Spec correctness note: the MCP 2025-06-18 spec separates concrete
  resources (`resources/list`) from templated ones
  (`resources/templates/list`); v0.2 ships only templates so
  `resources/list` returns an empty list while
  `resources/templates/list` carries the registry. `tools/call`
  validates `arguments` against the tool's JSON Schema 2020-12
  `inputSchema` via `jsonschema` (4.26+); a violation surfaces as
  `INVALID_PARAMS` through `McpInvalidParamsError`.
  `eager_import_mcp_modules()` (called from the FastAPI lifespan)
  walks every module under `mcp/tools/` and `mcp/resources/` via
  `pkgutil.iter_modules` so the side-effect-only registration calls
  at the top of each module run before the first request arrives —
  both subpackages are empty in T3 (T4 adds the first tool /
  resource). The dispatcher's `_McpHandler` signature widened from
  `(params)` to `(operator, params)` so registry handlers can apply
  RBAC; built-in lifecycle handlers (initialize, ping,
  notifications/initialized) take the operator but ignore it. The
  initialize response now advertises non-empty `capabilities` again
  (`tools={"listChanged": false}`, `resources={"listChanged": false,
  "subscribe": false}`) — T1's empty-envelope stance was paired with
  "no handlers registered"; T3 flips them back.

* MCP OAuth 2.1 resource-server protection (Task #247, G0.5-T2) —
  layers spec-conforming auth on top of the T1 dispatcher.
  `backend/src/meho_backplane/mcp/auth.py` houses the
  `verify_mcp_jwt` / `verify_mcp_jwt_and_bind` FastAPI dependencies,
  which reuse the chassis JWKS chain through the new
  `verify_jwt_for_audience` seam in `auth/jwt.py` but validate against
  the **MCP canonical URI** instead of the chassis `KEYCLOAK_AUDIENCE`
  — RFC 8707 §2 audience binding, so a token issued for the HTTP API
  surface cannot be replayed at `/mcp` and vice versa. On every 401
  the wrapper attaches the RFC 9728 §5.1
  `WWW-Authenticate: Bearer resource_metadata="<url>"` header so MCP
  clients can discover the resource-metadata document and walk the
  OAuth 2.1 + PKCE handshake against Keycloak. The unauthenticated
  metadata document lives at `/.well-known/oauth-protected-resource`
  in `backend/src/meho_backplane/api/well_known.py` — required fields
  per RFC 9728 §3 (`resource`, `authorization_servers`,
  `scopes_supported`, `bearer_methods_supported`) populated from the
  new `BACKPLANE_URL` + `MCP_RESOURCE_URI` settings. The dispatcher's
  `Depends(verify_mcp_jwt_and_bind)` runs before envelope parsing, so
  the request never reaches the JSON-RPC pipeline when auth fails;
  the binding side effect (`operator_sub` + `tenant_id` into structlog
  contextvars) is what makes `AuditMiddleware` write a row per `/mcp`
  request, replacing T1's implicit pass-through. T5 (#250) will layer
  MCP-specific audit semantics on `tools/call` / `resources/read`.
  Origin-header validation per the MCP transport DNS-rebinding warning
  is deferred to a later transport-hardening task — it needs
  `MCP_ALLOWED_ORIGINS` infrastructure that doesn't exist yet, and
  bundling it with T2 would over-scope the OAuth-RS work.

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
| `configure_logging` | `src/meho_backplane/logging.py` | Configures structlog: `merge_contextvars` → `add_log_level` → `TimeStamper(iso, utc)` → `dict_tracebacks` → `JSONRenderer`, writing to stdout. Idempotent. The logger factory is constructed as `PrintLoggerFactory()` with **no `file=` argument** — structlog's `PrintLogger.msg` then resolves `sys.stdout` lazily at write time (calls `print()` with no `file=` keyword whenever `self._file is sys.stdout`). Tests under `pytest`'s `capfd` swap `sys.stdout` for a wrapped fd that gets closed on test teardown; the lazy shape avoids capturing that wrapped fd at lifespan-startup time and surviving into a later test as a closed-fd `ValueError` from `cache_logger_on_first_use=True`. Production behaviour unchanged because the real process `sys.stdout` does not get rebound at runtime. |
| `RequestContextMiddleware` | `src/meho_backplane/middleware.py` | Pure-ASGI middleware. Per request: extracts/mints a `request_id`, clears any leftover contextvars and binds the new `request_id`, mirrors it onto the `X-Request-Id` response header, increments `http_requests_total{method,path,status}`, emits one `request_completed` JSON log line with method / path / status / duration_ms (which inherits any contextvars bound during the request, including `operator_sub` and `tenant_id` from `verify_jwt_and_bind`). |
| `verify_jwt_and_bind` | `src/meho_backplane/middleware.py` | FastAPI dependency wrapper around `verify_jwt`. On successful validation, binds `operator_sub` (the JWT's `sub` claim) and `tenant_id` (`str(operator.tenant_id)` — JSON renderer cannot serialise raw `uuid.UUID`) into structlog contextvars so every subsequent log line in the request scope carries both fields automatically. Authenticated routes use `Depends(verify_jwt_and_bind)` instead of `Depends(verify_jwt)` directly. Lives alongside the middleware because `RequestContextMiddleware`'s request-entry `clear_contextvars` call is what guarantees the bound keys do not leak across requests reusing the same asyncio task. `tenant_role` is intentionally *not* bound — it's enforced at the dependency layer (G0.1-T4 `require_role`) so handlers reach for the typed `Operator` instead of pulling roles out of contextvars. |
| `SENSITIVE_HEADERS` | `src/meho_backplane/middleware.py` | `frozenset({b"authorization", b"cookie", b"x-api-key"})`. The middleware never logs the values of these headers; redaction is enforced by *not* logging request headers at all in v0.1, with a `tests/test_observability.py` regression test. |
| `HTTP_REQUESTS_TOTAL` | `src/meho_backplane/metrics.py` | Module-level `prometheus_client.Counter` registered against the default registry. Labels: `method`, `path`, `status`. `path` is the matched FastAPI route template when available, bounding label cardinality. |
| `render_metrics` | `src/meho_backplane/metrics.py` | Returns `(body, content_type)` for the `/metrics` route. Pins `text/plain; version=0.0.4; charset=utf-8` — the legacy Prometheus format every scraper accepts (`prometheus_client>=0.21` advertises 1.0.0 in `CONTENT_TYPE_LATEST`, but 0.0.4 stays universally compatible). |
| `Settings` / `get_settings` | `src/meho_backplane/settings.py` | Pydantic v2 model + `lru_cache`-singleton accessor for the Keycloak knobs (`KEYCLOAK_ISSUER_URL`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_JWKS_CACHE_TTL_SECONDS`, `KEYCLOAK_JWT_LEEWAY_SECONDS`), the per-tenant JWT-claim-name knobs (`JWT_TENANT_CLAIM_NAME` default `tenant_id`, `JWT_TENANT_ROLE_CLAIM_NAME` default `tenant_role`), the RBAC stub-route gate (`MEHO_ENABLE_RBAC_TEST_ROUTE` default `False`; only `1` / `true` / `yes` / `on` are truthy — anything else, including `disabled`, evaluates to `False` so a typo cannot silently mount the routes in production), the Vault knobs (`VAULT_ADDR`, `VAULT_OIDC_ROLE`, `VAULT_OIDC_MOUNT_PATH`, `VAULT_NAMESPACE`, `VAULT_TIMEOUT_SECONDS`), and the database knobs (`DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_POOL_TIMEOUT`). `DATABASE_URL` is required and validated by a Pydantic `@field_validator` that rejects sync DSNs (`postgresql://`, `sqlite:///`, `postgresql+psycopg2://`) — only `postgresql+asyncpg://` and `sqlite+aiosqlite://` are accepted, matching ADR 0004's async-only mandate. Tests reset via `get_settings.cache_clear()`. |
| `create_engine_for_url` / `get_engine` / `get_sessionmaker` / `get_session` / `dispose_engine` | `src/meho_backplane/db/engine.py` | SQLAlchemy 2.x async engine + per-request session factory (Task #27). `get_engine` is lazy + cached; `get_session` is the FastAPI `Depends` that yields a transaction-bracketed `AsyncSession`. SQLite URLs (dev / aiosqlite) prune the `pool_size` / `pool_timeout` kwargs because StaticPool rejects them; Postgres URLs (asyncpg) keep them. `dispose_engine` is awaited from the lifespan shutdown so asyncpg's pool releases its connections cleanly. |
| `db_migration_probe` / `alembic_config` / `find_alembic_ini` / `_check_pgvector_extension` | `src/meho_backplane/db/migrations.py` | Async readiness probe + Alembic config helpers (Task #27, extended G0.4-T6 #263 with pgvector check). The probe compares `MigrationContext.configure(conn).get_current_revision()` against `ScriptDirectory.from_config(cfg).get_current_head()` over an `AsyncEngine.connect()`/`run_sync` pair; on the PostgreSQL dialect path it also runs `_check_pgvector_extension` (`SELECT 1 FROM pg_extension WHERE extname='vector'`) -- catches post-deploy drift where an operator manually dropped the extension or a backup restore brought back the schema without the catalog entry. Detail format: `revision=<sha>` on success (pgvector OK), `revision=<sha> pgvector=missing` when the extension is gone, `current=<sha> head=<sha>` on revision divergence, `check_failed: <ExcClass>` on connection / config errors (no operator-controllable URL substrings). SQLite dev/test driver skips the pgvector check (gated on `engine.dialect.name == "postgresql"`); the extension concept doesn't apply. |
| `Base` / `AuditLog` / `Tenant` / `Document` / `Target` / `OperationGroup` / `EndpointDescriptor` / `GraphNode` / `GraphEdge` / `BroadcastOverride` | `src/meho_backplane/db/models.py` | SQLAlchemy 2.x `DeclarativeBase` plus the `AuditLog` model (Task #28), the `Tenant` model (Task #231, G0.1-T1), the `Document` model (Task #258, G0.4-T1), the `Target` model (Task #252, G0.3-T1), the G0.6 operation-substrate models `OperationGroup` + `EndpointDescriptor` (Task #392, G0.6-T1), the G9.1 topology-graph substrate `GraphNode` + `GraphEdge` (Task #448, G9.1-T1), and the G6.3 PII-override-rule model `BroadcastOverride` (Task #378, G6.3-T1). `BroadcastOverride` (migration `0008`) is the per-tenant rule table the G6.3 broadcast resolver (T2 #379) reads at publish time to downgrade normally-full-detail ops to `aggregate` -- `tenant_id` UUID NOT NULL with a real `REFERENCES tenant(id)` FK (same brand-new-table precedent as `Document`); `op_id_pattern` glob (e.g. `vault.kv.*`); optional `scope_field` / `scope_value` pair (NULL = op-wide, allowlist `"namespace"` / `"target_name"` enforced at the Pydantic layer in T4 #381 rather than as a DB CHECK so future scope fields land without a migration); `detail` ∈ `"full"` / `"aggregate"` (Pydantic Literal at the API layer); `created_by_sub` captures the writing tenant admin's JWT `sub` for audit-trail; composite uniqueness via named `broadcast_override_tenant_unique_idx` on `(tenant_id, op_id_pattern, scope_field, scope_value)` (T4's upsert target) plus `broadcast_override_tenant_idx` b-tree on `tenant_id` (T2's per-tenant cache hydration index). Columns use portable `Uuid`, `JSON().with_variant(JSONB(), "postgresql")`, and `JSON().with_variant(PG_ARRAY(Text), "postgresql")` types so models and migrations run cleanly on both PG (production) and SQLite (dev/test). `AuditLog` indexes: `occurred_at`, `operator_sub`, `tenant_id`, `target_id`, `parent_audit_id` (all in `__table_args__`). `audit_log.tenant_id`, `audit_log.target_id`, and `audit_log.parent_audit_id` are **nullable in v0.2** and ship **without FKs** (same soft-FK discipline — deferred to v0.2.next backfill migrations). The `parent_audit_id` column (G0.6-T7 #398, migration `0006`) carries the composite parent's `audit_log.id` for each recursive child row dispatched via `DispatchChild`; top-level dispatches leave it NULL. `Tenant` has `slug` UNIQUE NOT NULL with the named `tenant_slug_idx` b-tree index. `Document` is the per-tenant retrievable shared substrate for G4/G5 with a real `REFERENCES tenant(id)` FK, `_PortableVector384` embedding, and GIN/IVFFlat indexes (PG-only, migration-managed). `Target` has `(tenant_id, name)` UNIQUE via `targets_tenant_name_idx`, a `(tenant_id, product)` b-tree index, and a GIN index on `aliases` (PG only). `OperationGroup` and `EndpointDescriptor` are the dispatcher-facing operation tables: `tenant_id` nullable (NULL = built-in/global, non-null = tenant-curated), bounded enums (`review_status` / `source_kind` / `safety_level`) enforced via CHECK constraints (portable across PG + SQLite), and uniqueness via **two partial unique indexes per table** (split on `WHERE tenant_id IS NULL` / `WHERE tenant_id IS NOT NULL`) so built-in and tenant-scoped rows with identical natural-key coordinates can coexist. `EndpointDescriptor.group_id` carries a real `REFERENCES operation_group(id) ON DELETE SET NULL` FK so deleting a group leaves descriptors dispatchable but ungrouped. `EndpointDescriptor.embedding` reuses the existing `_PortableVector384` decorator (same 384 dim as `Document.embedding`); nullable because T1 ships the column shape only and T4 populates it before retrieval. `GraphNode` and `GraphEdge` (migration `0007`) are the per-tenant topology-graph substrate in adjacency-list shape so PG 16's `WITH RECURSIVE … CYCLE` clause (§7.8.2.2 of the PG manual) can walk dependents / dependencies / paths without a graph extension: `graph_node.kind` is a closed enum (`target` / `vm` / `host` / `network` / `datastore` / `namespace` / `pod` / `service` / `ingress` / `node` / `principal` / `vault-role` / `vault-mount` / `volume`) enforced via `ck_graph_node_kind`; `(tenant_id, kind, name)` UNIQUE via `graph_node_tenant_kind_name_idx`; `target_id` carries a real `REFERENCES targets(id) ON DELETE SET NULL` FK (NULL for inner-graph nodes; SET NULL on target removal so the topology outlives the target row); `tenant_id` carries a real `REFERENCES tenant(id)` FK (NO ACTION — tenant deletion must clear the graph first). `graph_edge.kind` is the v0.2 auto-discoverable subset (`runs-on` / `mounts` / `routes-through` / `belongs-to`); `source` is `auto` / `curated`; both enforced via CHECK constraints. Endpoint FKs `from_node_id` / `to_node_id` cascade-delete (`ON DELETE CASCADE`) so a hard-deleted node does not leave dangling edges — refresh's soft-delete path nulls `last_seen` and leaves edges intact, so cascade only fires under tenant purges + test cleanup. `(tenant_id, from_node_id, to_node_id, kind)` UNIQUE via `graph_edge_tenant_endpoints_kind_idx`; `(tenant_id, from_node_id)` and `(tenant_id, to_node_id)` are named b-tree indexes that drive the recursive-CTE traversal in T4 (#451). `last_seen` is nullable on both tables — refresh writes a timestamp on each observation and nulls it once a node/edge has been absent past the threshold (the soft-delete signal kept queryable for G9.3 history replay but filtered out of default queries). |
| `AuditMiddleware` | `src/meho_backplane/audit.py` | Pure-ASGI middleware (Task #28, extended Task #233 G0.1-T3, extended Task #255 G0.3-T4). For every authenticated request (`operator_sub` present in contextvars) writes one `audit_log` row synchronously before yielding the response back to the send chain. Reads `tenant_id` off contextvars (bound by `verify_jwt_and_bind`) via `_resolve_tenant_id`, parses it back to `uuid.UUID`, and persists it to the `audit_log.tenant_id` column. G0.3-T4 (#255): also reads `target_id` via `_resolve_target_id` — bound by `resolve_target` at its single exit point (or by `create_target` directly for the POST route). `NULL` for routes that never call `resolve_target` (list, non-target routes). Missing or malformed `tenant_id` in contextvars logs `audit_missing_tenant_id` / `audit_malformed_tenant_id` at error level and writes the row with `tenant_id=NULL` rather than failing closed. Buffers `http.response.start`/`http.response.body` messages so the fail-closed path can replace them with a 500 `{"detail": "audit_write_failed"}` when the audit insert raises. Skips public surfaces and 401 paths by keying on the `operator_sub` contextvar's presence rather than path-matching. |
| `0001_create_audit_log` | `backend/alembic/versions/0001_create_audit_log.py` | First migration on the schema (Task #28). Creates the `audit_log` table plus `audit_log_occurred_at_idx` and `audit_log_operator_sub_idx`. PG gets `gen_random_uuid()` / `now()` / `'{}'::jsonb` server defaults; SQLite branches skip them and rely on the ORM Python-side defaults. Downgrade drops the table — the only revertible operation here because no production data exists yet; subsequent migrations land under the additive-only discipline enforced by Task #29's CI guard. |
| `0002_create_tenant_and_audit_tenant_id` | `backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py` | G0.1-T1 (Task #231) schema migration: creates the `tenant` table (id UUID PK, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at timestamptz) plus the `tenant_slug_idx` b-tree index, and adds the **nullable** `tenant_id uuid` column on `audit_log` plus the `audit_log_tenant_id_idx` b-tree index. Dialect-portable: PG gets `gen_random_uuid()` / `now()` server defaults on `tenant.{id,created_at}`; SQLite leaves them to the ORM. **No FK from `audit_log.tenant_id` to `tenant.id` in v0.2** — the soft column shape keeps the migration reversible without cascade choices and lets v0.2.next ship the backfill + NOT NULL + REFERENCES tightening as one coordinated change. `downgrade()` reverses everything in symmetric order (drop indexes → drop column → drop tenant table); the destructive ops are confined to `downgrade()` and the CI guard (`scripts/ci/check_migration_compat.py`) only inspects `upgrade()` so the discipline holds. |
| `0003_create_documents_with_pgvector` | `backend/alembic/versions/0003_create_documents_with_pgvector.py` | G0.4-T1 (Task #258) retrieval substrate. Enables the PostgreSQL `vector` extension (PG-only `CREATE EXTENSION IF NOT EXISTS vector`); creates the `documents` table — `id` UUID PK, `tenant_id` UUID NOT NULL with a real `REFERENCES tenant(id)` FK (unlike audit_log's soft column — `documents` is a brand-new table so the FK lands at create-time without backfill or cascade trade-offs), `source` / `source_id` / `kind` / `body` / `body_hash` text NOT NULL, `tokens` nullable int, `embedding` as `vector(384)` on PG / `Text` on SQLite (via `with_variant`), `metadata` JSONB on PG / JSON on SQLite, `created_at` / `updated_at` timestamptz. Installs four indexes: `documents_tenant_source_id_idx` unique composite (`tenant_id`, `source`, `source_id`) for upsert-by-natural-key, `documents_body_hash_idx` btree for change-detection short-circuit during refresh, plus **PG-only** `documents_body_fts_idx` GIN over `to_tsvector('english', body)` (BM25) and `documents_embedding_idx` IVFFlat with `vector_cosine_ops` and `WITH (lists = 100)` (cosine). The two PG-only indexes are emitted via raw `op.execute` (no clean Alembic API for expression-based GIN / IVFFlat operator-class + `WITH` parameters) and are deliberately **not** in `Document.__table_args__` so SQLite's `create_all` stays clean. The chassis Postgres image must ship the extension — `pgvector/pgvector:pg16` (or equivalent managed-PG offering: RDS, Cloud SQL, Azure DB) — so the testcontainers suites and the integration conftest pin that image; the v0.1 `postgres:16-alpine` image fails fast on the `CREATE EXTENSION` step (the deploy prerequisite surfaces at migration time, not first retrieval). Downgrade drops the table + its indexes; the `vector` extension is **deliberately left installed** (other tenants of the same PG cluster may share it, and `DROP EXTENSION CASCADE` would silently drop their vector columns). IVFFlat against an empty table produces non-useful centroids until backfill — T3/T4's runbook documents the `REINDEX INDEX documents_embedding_idx` step after the first batch. |
| `0006_add_audit_log_parent_audit_id` | `backend/alembic/versions/0006_add_audit_log_parent_audit_id.py` | G0.6-T7 (Task #398) audit-tree linkage. Adds `audit_log.parent_audit_id uuid NULL` + `audit_log_parent_audit_id_idx` b-tree index. Soft column (no FK to `audit_log.id` in v0.2 — self-referential FKs on append-only audit tables are painful to retrofit, so the v0.2.next tightening migration handles backfill + `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT` in one atomic change). Populated by the G0.6 dispatcher when a composite handler issues a recursive `dispatch_child(...)` call; top-level dispatches leave it NULL. Drives recursive-CTE traversal at audit-replay time (G8.1 / G8.2). |
| `0005_create_endpoint_descriptor` | `backend/alembic/versions/0005_create_endpoint_descriptor.py` | G0.6-T1 (Task #392) operation-substrate schema. Creates `operation_group` (per-product/version/impl-id grouping; carries the LLM-summarised `when_to_use` blurb the `list_operation_groups` meta-tool returns) and `endpoint_descriptor` (one row per operation the dispatcher can route — `source_kind` ∈ `ingested` / `typed` / `composite`, with `method`+`path` for ingested HTTP ops and `handler_ref` for typed/composite; `parameter_schema`/`response_schema` JSONB for validation; `safety_level` + `requires_approval` for the policy gate; `embedding vector(384)` on PG / `Text` on SQLite via `_PortableVector384` for hybrid retrieval). Bounded enums (`review_status` / `source_kind` / `safety_level`) enforced via CHECK constraints — same portable pattern `ck_targets_auth_model` uses. Uniqueness via **two partial unique indexes per table** split on `WHERE tenant_id IS NULL` (built-in / global rows) and `WHERE tenant_id IS NOT NULL` (tenant-scoped rows, with `tenant_id` in the key for cross-tenant isolation); a single composite `UNIQUE (tenant_id, ...)` would not catch duplicate built-in rows because SQL NULL != NULL. `endpoint_descriptor.group_id` carries `ON DELETE SET NULL` so deleting an `operation_group` leaves descriptors dispatchable but ungrouped. Lookup index `endpoint_descriptor_lookup_idx` btree on `(product, version, impl_id, group_id, is_enabled)` drives the group-scoped query the dispatcher / `search_operations` meta-tool issues. **PG-only** `endpoint_descriptor_bm25_idx` GIN over `to_tsvector('english', coalesce(summary, '') \|\| ' ' \|\| coalesce(description, ''))` and `endpoint_descriptor_embedding_idx` IVFFlat with `vector_cosine_ops` / `WITH (lists = 100)` — same dialect-guarded raw-SQL pattern migration `0003` uses. Downgrade drops both tables + every index in reverse order; the FK on `endpoint_descriptor.group_id` drops with the table so the `operation_group` table can be removed last. T1 ships **the empty schema**; population is T4 (`register_typed_operation()`) + G0.7 (spec ingestion) territory. |
| `meho_backplane.db.migrate.main` | `backend/src/meho_backplane/db/migrate.py` | Helm pre-install / pre-upgrade Job entrypoint (Task #29). Calls `alembic.command.upgrade(cfg, "head")` against the `alembic_config()` resolved by `db/migrations.py`. Returns 0 on success / 1 on failure with `migration_failed: <ExcClass>: <msg>` on stderr. No CLI flags by design — schema target is always `head`, and forward-only is enforced by not exposing `downgrade`. |
| `check_migration_compat` | `scripts/ci/check_migration_compat.py` | CI guard (Task #29). Scans every `backend/alembic/versions/*.py` migration's `upgrade()` function for destructive patterns via a dual AST + regex detector; exit 0 on a clean tree, 1 on any violation. Honours an optional positional argument (a versions directory) so the test suite can point the guard at synthetic fixtures without monkeypatching module state. Workflow trigger is path-filtered to `backend/alembic/versions/**` plus the script itself. |
| `backend/alembic.ini` + `backend/alembic/env.py` + `backend/alembic/script.py.mako` | repo paths | Alembic configuration (Task #27). `env.py` follows the upstream async cookbook: `async_engine_from_config` + `connection.run_sync(do_run_migrations)`. URL is sourced from `DATABASE_URL` so the migration runner and the running backplane share one knob. `versions/` ships empty; first migration lands in T28. |
| `Operator` | `src/meho_backplane/auth/operator.py` | Frozen pydantic v2 model carrying validated claims (`sub`, `name`, `email`, `raw_jwt`, `tenant_id: UUID`, `tenant_role: TenantRole`). Returned by `verify_jwt`; consumed by every authenticated route from G2.2-T3 onward. `raw_jwt` is preserved verbatim for G2.2-T2's Vault forward-auth. `tenant_id` / `tenant_role` (G0.1-T2) are required — the model can no longer be constructed without them, so any future regression that drops the claim extraction surfaces as a pydantic `ValidationError` rather than a silent anonymous request. |
| `TenantRole` | `src/meho_backplane/auth/operator.py` | `StrEnum` (stdlib 3.11+) with exactly three values in v0.2: `tenant_admin` / `operator` / `read_only`. Closed-set so the RBAC primitive (Task #234, `require_role`) can match exhaustively. Widening is a v0.2.next decision; a regression test in `test_auth_jwt.py` pins the spelling so accidental additions surface in CI. |
| `verify_jwt` | `src/meho_backplane/auth/jwt.py` | FastAPI dependency: parses `Authorization: Bearer ...`, fetches/caches Keycloak's JWKS, validates signature + `iss` + `aud` + `exp` (with leeway), refreshes JWKS on a kid miss, extracts `tenant_id` (UUID) and `tenant_role` (`TenantRole`) under the settings-controlled claim names, and returns an `Operator`. Every failure mode collapses to a terse 401; tenant-claim failures emit one of four distinct structlog events (`missing_tenant_claim` / `missing_tenant_role_claim` / `malformed_tenant_claim` / `unknown_tenant_role`) plus a matching `detail` token so on-call telemetry maps cleanly to remediation. |
| `keycloak_readiness_probe` | `src/meho_backplane/auth/jwt.py` | Synchronous probe registered with the readiness registry at app lifespan startup. Hits `{issuer}/.well-known/openid-configuration` then `jwks_uri`; failure detail surfaces only the exception class name to avoid leaking issuer URLs into 503 payloads. |
| JWKS cache | `src/meho_backplane/auth/jwt.py` (`_jwks_cache`, `_jwks_fetched_at`, `_jwks_lock`) | Module-level dict + monotonic-fetched timestamp + asyncio lock. TTL-bounded (default 5 min) and kid-rotation refreshed (one forced re-fetch per request on a kid miss). Single-worker design; v0.2 may move to Redis when multi-worker uvicorn is needed. |
| `vault_client_for_operator` | `src/meho_backplane/auth/vault.py` | Async context manager: builds an `hvac.Client` from settings, performs `client.auth.jwt.jwt_login(role, jwt, path)` against the configured mount path, yields the authenticated client, and revokes the issued token on exit (best-effort). Every blocking hvac call runs through `asyncio.to_thread` because hvac is `requests`-based and FastAPI does not auto-offload sync I/O inside `async def` callables. Per-request login by design (v0.1); v0.2 may add a per-operator cache. |
| `vault_readiness_probe` | `src/meho_backplane/auth/vault.py` | Readiness probe registered at app lifespan startup. Delegates to `VaultConnector().probe(VaultTarget())` (T5 G0.2-T5 refactor; lazy import avoids circular dependency). Returns `ProbeResult(name="vault", ok=..., detail=connector_probe.reason)`. Flips `/ready` red when `/sys/health` is unreachable, sealed, or uninitialized. |
| `VaultClientError` / `VaultUnreachableError` / `VaultRoleDeniedError` | `src/meho_backplane/auth/vault.py` | Backplane-side exception hierarchy. Callers catch `VaultClientError` for a single error response shape, or one of the subclasses to map to specific HTTP statuses. The hierarchy lets consumers avoid importing `hvac` directly. |
| `Connector` (ABC) | `src/meho_backplane/connectors/base.py` | Abstract base for every MEHO connector. Three abstract async methods: `fingerprint(target) -> FingerprintResult`, `probe(target) -> ProbeResult`, `execute(target, op_id, params) -> OperationResult`. Subclasses set `product` (e.g. `"vsphere"`, `"vault"`, `"bind9"`); the G0.6-T3 (#394) registry v2 metadata adds four backward-compatible class attrs that defaulted-subclasses inherit unchanged: `version: str = ""`, `impl_id: str = ""`, `supported_version_range: str \| None = None` (PEP 440-style, e.g. `">=8.5,<10.0"`), `priority: int = 0` (resolver tie-break — higher wins). Defaults preserve v1 single-product registry behaviour so shipped `VaultConnector` / `KubernetesConnector` keep working without modification; G3.x reframe Initiatives override these as connectors gain version/impl discrimination. G0.6-T4 (#395) `register_typed_operation()` populates per-op metadata on top of the class-level advertisements. The `Target` type annotation is a forward reference — resolved in G0.3 when the Target model lands. |
| `VaultConnector` / `VaultTarget` | `src/meho_backplane/connectors/vault/connector.py` | G0.2-T5 reference implementation (#244). `product="vault"`. `fingerprint` calls `_build_client` + `_to_thread_read_health` and returns `FingerprintResult(vendor="hashicorp", product="vault", version=..., extras={cluster_id, sealed, standby, ...})`. `probe` calls the same unauthenticated health endpoint and classifies the response via `_classify_health_response`. `execute` dispatches `vault.kv.read` (and future ops) via `OP_MAP`. Auth model: `shared_service_account` — every operator's JWT is forwarded to Vault's JWT/OIDC auth method via `vault_client_for_operator`. `VaultTarget` is a pre-G0.3 stand-in (only `raw_jwt: str \| None`); replace with the G0.3 `Target` model when #224 lands. |
| `OP_MAP` / `vault_kv_read` | `src/meho_backplane/connectors/vault/ops.py` | Vault op-map for G0.2-T5. `vault.kv.read` reads a KV v2 secret via OIDC-forwarded JWT; returns `OperationResult(status="ok", result=<data dict>, extras={"version": N})` on success. Login failures (`VaultClientError`) return `extras["phase"]="login"`; read failures return `extras["phase"]="read"`. Future ops (`vault.kv.write`, `vault.kv.list`, `vault.policy.read`, `vault.transit.encrypt`) are out of scope for T5. |
| `connectors/vault` package registration | `src/meho_backplane/connectors/vault/__init__.py` | Auto-registers `VaultConnector` under `"vault"` in the connector registry when the package is imported. `_eager_import_connectors` at lifespan startup triggers this via `importlib.import_module`. |
| `tests/test_connectors_vault.py` | `backend/tests/test_connectors_vault.py` | G0.2-T5 (#244) unit suite. Covers registry registration, fingerprint shape from `/v1/sys/health`, probe ok/sealed/uninitialized/standby/unreachable, `execute` happy path (`vault.kv.read`), unknown op-id, missing/non-string path param, login-phase errors (`VaultUnreachableError` / `VaultRoleDeniedError`), read-phase errors, and malformed hvac payload (`KeyError` → read-phase error). All 19 tests use `monkeypatch.setattr(vault_module, "_build_client", ...)` as the single seam — no real HTTP, no Vault container. |
| `register_connector` / `get_connector` / `all_connectors` | `src/meho_backplane/connectors/registry.py` | Module-level connector registry (G0.2-T2). `register_connector(product, cls)` maps a product slug (e.g. `"vsphere"`) to a `Connector` subclass; duplicate registration raises `RuntimeError` (programming bug — fail fast at deploy). `get_connector(product)` returns the registered class or `None`. `all_connectors()` returns a snapshot dict for diagnostics. Each successful registration emits a `connector_registered` structlog line with `product` and `cls` fields. All three are re-exported from `meho_backplane.connectors` (package root). G0.6-T2 (#393) bridges to v2: every v1 registration now also writes `(product, "", "")` into the v2 table and emits a `connector_registered_v1_compat` deprecation log line pointing at `register_connector_v2`. |
| `register_connector_v2` / `all_connectors_v2` / `list_connector_impls` | `src/meho_backplane/connectors/registry.py` | G0.6-T2 (#393) v2 registry keyed on `(product, version, impl_id)` so multiple implementations per product can coexist (e.g. `vmware-pyvmomi-7.0` + `vmware-rest-9.0`). `register_connector_v2(*, product, version, impl_id, cls)` is keyword-only — three positional strings invite ordering bugs. Duplicate three-tuple raises `RuntimeError`. `all_connectors_v2()` returns the full snapshot (including v1 entries as `(product, "", "")`); `list_connector_impls()` returns the sorted key list for diagnostics. v2-only entries are invisible to v1 `get_connector` (which keys on product alone); they're only resolvable via `resolve_connector`. |
| `resolve_connector` / `NoMatchingConnector` / `AmbiguousConnectorResolution` | `src/meho_backplane/connectors/resolver.py` | G0.6-T2 (#393) target → connector class resolver. Reads `target.product`, `target.fingerprint.version`, and `target.preferred_impl_id` (G0.3 amendment per #224 — read via `getattr` until the column lands). Filters the v2 registry on product + `supported_version_range` membership (via `packaging.specifiers.SpecifierSet`), then runs the tie-break ladder: (1) most-specific-version-match — bounded ranges beat half-bounded beat unbounded; among bounded, smaller `(upper - lower)` span wins; (2) operator/tenant preference — `target.preferred_impl_id` selects when specificity ties; (3) class `priority` — higher wins as final tie-break. Zero candidates → `NoMatchingConnector(LookupError)`; ≥2 after the full ladder → `AmbiguousConnectorResolution(LookupError)` with a sorted `candidates: list[tuple[str, str, str]]` for diagnostics. Both are `LookupError` subclasses so broad `except LookupError` patterns keep working. Emits a `connector_resolved` structlog line with `tie_break=<reason>` on success. See [docs/architecture/connector-resolution.md](../architecture/connector-resolution.md) for the diagrammed ladder + three worked examples. |
| `_eager_import_connectors` | `src/meho_backplane/connectors/registry.py` | Called from the app lifespan at startup. Uses `pkgutil.iter_modules` to discover every subpackage under `meho_backplane.connectors/`, then `importlib.import_module` to import each one. Import side-effects (`register_connector(...)` at module top-level in each product's `__init__.py`) populate the registry before the first request arrives. No-ops gracefully when no connector subpackages are present (current state until G0.2-T5+). |
| `AuthModel` (StrEnum) | `src/meho_backplane/connectors/schemas.py` | Per-target identity model: `IMPERSONATION` / `SHARED_SERVICE_ACCOUNT` / `PER_USER`. String values are the canonical v0.1-spec identifiers (`"impersonation"`, `"shared_service_account"`, `"per_user"`). Stored on the Target model (G0.3); per-product defaults; per-target override. |
| `FingerprintResult` / `ProbeResult` / `OperationResult` / `ResultHandle` | `src/meho_backplane/connectors/schemas.py` | Frozen pydantic v2 result models for the three connector operations plus the JSONFlux result-handle. `FingerprintResult` carries the full product identity shape (vendor / product / version / build / edition / reachable / probed_at / probe_method / extras). `ProbeResult` is the lightweight reachability + auth-challenge verdict (ok / reason / latency_ms / probed_at). `OperationResult` carries the typed op result as raw JSON dict or list (`result` field) plus status / op_id / error / duration_ms / extras, **and** an optional `handle: ResultHandle \| None = None` field — set by JSONFlux-aware reducers when the response is large enough to spill out-of-band (v0.2's `PassThroughReducer` always leaves it `None`). `ResultHandle` (G0.6-T6, #397) is the addressable reference to a spilled payload: `handle_id` (UUID), `summary_md` (reduced markdown summary), `schema_` (JSON Schema 2020-12 of the underlying payload — trailing underscore avoids collision with Pydantic's deprecated `BaseModel.schema()`), `total_rows` (optional row count), `sample_rows` (optional first-N preview), `ttl_seconds` (handle lifetime in the backing store). The model lives here rather than in `operations/reducer.py` so `OperationResult` can carry it as a first-class field without an `operations → connectors → operations` import cycle; `operations.reducer` re-exports `ResultHandle` for the documented public path. All four are importable from `meho_backplane.connectors` (package root). Note: `connectors.ProbeResult` is distinct from `health.ProbeResult` (the readiness-probe dataclass). |
| `Target` / `TargetSummary` / `TargetCreate` / `TargetUpdate` | `src/meho_backplane/targets/schemas.py` | Pydantic v2 schemas for the G0.3 targets surface (Task #253). `Target` is the full read shape (all columns, frozen). `TargetSummary` is the short list shape (id / name / aliases / product / host, frozen). `TargetCreate` is the POST body with field validation (`name` min_length=1, `port` ge=1 le=65535). `TargetUpdate` is the PATCH body — all fields optional; `name` and `product` are absent (rename = delete + create). `AuthModel` is re-exported from `meho_backplane.connectors.schemas` so consumers can import it from either location. All four schemas importable from the package root (`meho_backplane.targets`). |
| `resolve_target` / `TargetNotFoundError` / `AmbiguousTargetError` | `src/meho_backplane/targets/resolver.py` | Universal `query → Target ORM row` resolver (Task #253, extended G0.3-T4 #255). Algorithm: (1) exact name match `WHERE tenant_id = ? AND name = ?`; (2) element-equality alias match (`query = ANY(aliases)` on PG, Python-side `in` on SQLite); (3) prefix-ILIKE near-miss (up to 5, name + aliases on PG, name-only on SQLite) → `TargetNotFoundError`. `AmbiguousTargetError` (HTTP 409) fires when multiple rows match the alias step. T4 refactored to a single exit point: on every successful return `structlog.contextvars.bind_contextvars(target_id=str(target.id))` is called + a `target_resolved` log line emitted. `AuditMiddleware` reads this contextvar to populate `audit_log.target_id`. Both error classes extend `HTTPException` for clean FastAPI propagation; CLI verbs catch them and render human-readable suggestions. |
| `HttpConnector` | `src/meho_backplane/connectors/adapters/http.py` | Abstract HTTP-API connector base (G0.2-T3). Every HTTP-based vendor connector (vSphere, NSX, Harbor, Hetzner Robot, etc.) inherits this and overrides `auth_headers(target, raw_jwt) -> dict[str, str]` plus the three ABC methods. Provides: (1) per-target `httpx.AsyncClient` pooling — one client per `target.name`, created lazily, reused across all ops, closed via `aclose()`; (2) retry policy — 3 retries on idempotent verbs (GET, HEAD, OPTIONS) with exponential backoff (0.5 s → 1 s → 2 s) via `tenacity`; 4xx responses are not retried, 5xx + connection errors are; (3) cert-bundle passthrough — `httpx` honours `SSL_CERT_FILE` natively so no custom cert logic is needed; (4) `_get_json()` / `_post_json()` helpers — `_get_json` routes through the retried `_request_json`; `_post_json` calls the client directly (no automatic retry for non-idempotent verbs). Auth-bearer plumbing per `target.auth_model` is vendor-specific — `auth_headers()` raises `NotImplementedError` on the base; per-vendor overrides land in G3.1+ connectors. |
| `KubernetesConnector` / `product_from_git_version` | `src/meho_backplane/connectors/kubernetes/connector.py` | G3.2-T1 (#321) canary skeleton, refactored against the G0.6 substrate by #391. Subclasses `Connector` with the v2 triple `product="k8s"` / `version="1.x"` / `impl_id="kubernetes-asyncio"`. `fingerprint(target)` calls `VersionApi.get_code()` against a cached `ApiClient` and maps the returned `gitVersion` suffix to a distribution slug (`rke2` / `k3s` / `eks` / `gke` / `aks` / `vanilla`) via `product_from_git_version`. `probe(target)` is **kubeconfig-free**: TLS GET against `https://{host}:{port or 6443}/readyz` with `verify=False` (probe is reachability, not auth — 200 / 401 both count as ok; auth surfaces at op time). `about(target, params)` is the canary typed op registered against the G0.6 substrate — reuses `VersionApi.get_code()` and returns a flat dict with the cluster's product / gitVersion / platform / build_date. `register_operations()` classmethod walks `KUBERNETES_OPS` and upserts each row via `register_typed_operation()`; called from the lifespan after `_eager_import_connectors()`. `execute(target, op_id, params)` is the dispatcher shim — global `endpoint_descriptor` lookup, JSON Schema validation via the dispatcher's helpers, `import_handler` + bound-method rebind for the resolved handler, then `(target, params)` invoke. Unknown op_ids return the dispatcher's structured `unknown_op` envelope (`error_code=unknown_op`, `known_op_count=<int>`). Per-target `ApiClient` cache guarded by a single `asyncio.Lock`; `aclose()` closes every cached client and clears the cache (idempotent). |
| `KubernetesTargetLike` / `KubeconfigLoader` / `load_kubeconfig_from_vault` / `parse_kubeconfig_yaml` | `src/meho_backplane/connectors/kubernetes/kubeconfig.py` | Auth-flow plumbing for the Kubernetes connector. `KubernetesTargetLike` is a structural `Protocol` capturing the minimum target shape the connector reads — `name`, `host`, `port`, `secret_ref` — so it can land ahead of the concrete `Target` model (G0.3 / #224). `KubeconfigLoader` is the async callable type the connector takes as a constructor argument; tests inject a mock returning a pre-built dict, production injects a wrapper around `load_kubeconfig_from_vault`. The default `load_kubeconfig_from_vault` raises `NotImplementedError` until G0.3 lands + the operator-context Vault read path is wired (T2+); the error message names the missing dependency and the override point so the failure mode is self-documenting. `parse_kubeconfig_yaml` is the YAML-text → kubeconfig-dict helper; rejects non-mapping YAML (empty file, scalar) with `ValueError`. |
| `connectors/kubernetes` package registration | `src/meho_backplane/connectors/kubernetes/__init__.py` | Imports the public surface (`KubernetesConnector`, `product_from_git_version`, the kubeconfig types, `KUBERNETES_OPS` / `KubernetesOp`) and registers the connector against both the v1 (`register_connector("k8s", ...)`) and v2 (`register_connector_v2(product="k8s", version="1.x", impl_id="kubernetes-asyncio", ...)`) registries. The v1 entry stays temporarily for chassis-route backward compat with `POST /api/v1/connectors/{product}/{op_id}`; T11 (#412) deprecates that route in favour of `/api/v1/operations/call` against the v2 entry. |
| `KUBERNETES_OPS` / `KubernetesOp` | `src/meho_backplane/connectors/kubernetes/ops.py` | Typed-op metadata table for the K8s connector. Each `KubernetesOp` dataclass row carries the kwargs `register_typed_operation()` consumes: `op_id`, `handler_attr` (the connector method name), summary / description / parameter_schema / response_schema, `group_key`, `tags`, `safety_level`, `requires_approval`, `llm_instructions`. v0.2 ships one entry: `k8s.about`. The full 13-op read surface (`k8s.pod.list`, `k8s.deployment.list`, etc.) lands in G3.2-T2..T5 (#320) against the same pattern. |
| `tests/test_connectors_k8s_auth.py` | `backend/tests/test_connectors_k8s_auth.py` | G3.2-T1 (#321) unit suite, updated for #391. Covers `product_from_git_version` across all six distro mappings, the fingerprint shape against a mocked `VersionApi.get_code()` response, probe ok/not-ok across HTTP 200 / 401 / 503 / transport-error, the default-port fallback when `target.port is None`, `execute` returning the dispatcher's structured `unknown_op` envelope (`error_code=unknown_op`, `known_op_count=<int>`) for an op_id with no descriptor row, `_get_api_client` caching (same target reuses, different targets get distinct clients, loader called once per target), `aclose` closing every cached client and being idempotent, and `parse_kubeconfig_yaml` rejecting non-mapping YAML. The `kubernetes_asyncio` and `httpx` modules are mocked via `unittest.mock.patch` on the import path inside the connector module so the gate runs without Docker. The default `load_kubeconfig_from_vault` is exercised once to lock its `NotImplementedError` contract until G0.3 / the live override lands. |
| `tests/test_connectors_k8s_dispatcher_shim.py` | `backend/tests/test_connectors_k8s_dispatcher_shim.py` | G0.6 refactor (#391) unit suite covering the dispatcher-shim contract: registry v2 class attrs, both-registry-layers package registration, `register_operations` upsert + idempotency (mocked embedding), and the four shim branches via the `k8s.about` op (`unknown_op` / `ok` happy path / `invalid_params` on schema fail / `connector_error` on handler raise). The embedding service is patched at the `typed_register.encode_endpoint_text` seam so the test never touches fastembed. |
| `tests/integration/test_connectors_k8s_k3d.py` | `backend/tests/integration/test_connectors_k8s_k3d.py` | G3.2-T1 (#321) live integration suite. Boots a single `rancher/k3s` container via `testcontainers.k3s.K3SContainer` (image name env-overridable via `MEHO_TEST_K3S_IMAGE`), pulls the kubeconfig the container exposes, parses it via `parse_kubeconfig_yaml`, and exercises fingerprint / probe / `_get_api_client` caching against the running API server. Four tests: (1) fingerprint maps to `product="k3s"` with a populated `gitVersion`; (2) probe returns `ok=True` against the live `/readyz`; (3) probe against an unreachable host (port 1) returns `ok=False` with an informative reason; (4) second fingerprint call against the same target reuses the cached `ApiClient`. Skip path mirrors `tests/integration/conftest.py`: Docker socket missing → skip; k3s container start failure (no privileged, cgroup mismatch) → skip with the underlying exception class name. The integration job that runs this in CI ships with T6 of #320; today the test collects + skips on no-Docker sandboxes and runs to completion on Docker-having runners. |
| `api/v1/health.router` (`/api/v1/health`) | `src/meho_backplane/api/v1/health.py` | Authenticated federation-proof endpoint (Task #24, extended in Task #27 and T5 G0.2-T5). `GET` handler runs through `Depends(verify_jwt_and_bind)`, dispatches `vault.kv.read` via `get_connector("vault").execute(VaultTarget(raw_jwt=...), "vault.kv.read", {"path": "meho/test/federation"})`, invokes `db_migration_probe()` to populate `db.migrated`, and returns `HealthResponse` (operator identity + vault status + db status). Vault unreachable / role denied / read failure / DB unreachable / revision diverged all surface as structured fields on a 200 response — never 5xx. |
| `require_role` | `src/meho_backplane/auth/rbac.py` | RBAC primitive (Task #234, G0.1-T4): function factory returning a FastAPI dependency that runs after `verify_jwt_and_bind` and rejects operators below a minimum `TenantRole` with HTTP 403 `insufficient_role` plus a structured `insufficient_role` log line carrying `operator_sub` / `actual_role` / `required_role`. Role ranking is **explicit** (a private `_ROLE_ORDER` tuple — `read_only` < `operator` < `tenant_admin`), not implicit in the StrEnum, so a future enum reorder cannot silently invert ranking. The minimum-role rank is resolved at factory call time so a typo or an enum widening that misses `_ROLE_ORDER` surfaces as an import-time `ValueError` rather than a per-request 500. Returns the validated `Operator` so handlers that need both the role gate and the operator instance can declare a single `Depends`. |
| `api/v1/rbac_test.router` (`/api/v1/rbac-test/*`) | `src/meho_backplane/api/v1/rbac_test.py` | End-to-end stub for `require_role` (Task #234): two GET endpoints (`/api/v1/rbac-test/admin` gated by `require_role(TENANT_ADMIN)`, `/api/v1/rbac-test/operator` gated by `require_role(OPERATOR)`). Mounted only when `Settings.enable_rbac_test_route` is `True` (env var `MEHO_ENABLE_RBAC_TEST_ROUTE=1`); production deploys leave the routes genuinely unmounted (404), CI flips the flag for the RBAC integration job. The flag is read at FastAPI app construction time — flipping it post-import has no effect; tests that need the routes build their own `FastAPI`. |
| `api/v1/targets.router` (5 routes under `/api/v1/targets`) | `src/meho_backplane/api/v1/targets.py` | CRUD surface for the G0.3 targets registry (Task #254). 5 routes, all tenant-scoped via `operator.tenant_id` from the JWT — cross-tenant reads are impossible by construction. `GET /api/v1/targets` lists targets as `list[TargetSummary]`, keyset-paginated (`?cursor=<last-name>`, `?limit=`, `?product=` filter); gated on `require_role(OPERATOR)`. `GET /api/v1/targets/{name}` resolves via `resolve_target` (alias-aware, near-miss 404 detail); same gate. `POST /api/v1/targets/{name}/probe` resolves the target then calls `get_connector(product).probe(target)` — 501 when no connector registered; same gate. `POST /api/v1/targets` creates a target (201 / 409 on duplicate name); gated on `require_role(TENANT_ADMIN)`. `PATCH /api/v1/targets/{name}` partially updates via `body.model_dump(exclude_unset=True)` and always refreshes `updated_at`; `name` and `product` are absent from `TargetUpdate` (rename = delete + create, per v0.2 decision); same admin gate. Routes that call `resolve_target` have `target_id` bound into contextvars at the resolver's single exit point (G0.3-T4 #255). `create_target` binds `target_id=str(t.id)` directly after adding the new row. `list_targets` does not call `resolve_target`, so `audit_log.target_id` is `NULL` for list requests — the slot is initialised to `None` by `verify_jwt_and_bind`. `model_dump(mode="json")` is used in `TargetNotFoundError` / `AmbiguousTargetError` so UUID fields in the `matches` list are JSON-safe strings when FastAPI serialises the 404/409 detail. |
| `list_operation_groups` / `search_operations` / `call_operation` / `describe_descriptor` (meta-tools) + `api/v1/operations.router` (4 routes) + `mcp/tools/operations` (3 MCP tools) | `src/meho_backplane/operations/meta_tools.py` (+ `_search.py`), `src/meho_backplane/api/v1/operations.py`, `src/meho_backplane/mcp/tools/operations.py` | G0.6-T8 (#399) operation meta-tool surface. Three async meta-tool handlers ship in `meta_tools.py`: `list_operation_groups(operator, {"connector_id"})` returns enabled `OperationGroup` rows for the connector's `(product, version, impl_id)` triple — tenant-scoped (built-in NULL rows + this tenant's curated rows) — with an `operation_count` per group sourced from a single follow-up `SELECT group_id FROM endpoint_descriptor WHERE group_id IN (...) AND is_enabled` aggregated in Python (one SQL round-trip, group-by in-process for PG/SQLite portability). Unknown `connector_id` returns `{"groups": []}` rather than 404 — empty is operationally meaningful for the agent. `search_operations(operator, {connector_id, query, group?, limit?})` runs hybrid BM25 + cosine RRF over `endpoint_descriptor` via `_search.hybrid_search`: dialect-aware (PG path uses `ts_rank_cd(to_tsvector(...))` against the `endpoint_descriptor_bm25_idx` GIN expression index + `embedding <=> CAST(:emb AS vector)` against the IVFFlat index; SQLite fallback ranks by substring-term count + Python-side cosine over each row's stored embedding), tenant boundary enforced (`tenant_id IS NULL OR tenant_id = :tenant`), optional `group` filter resolves to `operation_group.id` via tenant-then-global precedence and short-circuits to `{"hits": []}` on unknown key. RRF fusion math matches `retriever._rrf_fuse` (`1.0 / (RRF_K + rank)`, 1-based ranks, `RRF_K=60`, `CANDIDATE_LIMIT=50`); per-signal `bm25_score` / `cosine_score` surface in `OperationSearchHit` so operators can debug ranking quality. `limit` is clamped to `SEARCH_LIMIT_MAX = 50`. `call_operation(operator, {connector_id, op_id, target?, params})` resolves a partial `{"name": ...}` target descriptor via `targets.resolver.resolve_target` (alias-aware, tenant-scoped) then invokes `operations.dispatch` — the substrate's single entry point — and returns `OperationResult.model_dump(mode="json")` verbatim (structured-error envelope rides on a successful HTTP response; never raises). Missing `target.name` (sent as `{"target": {}}`) raises `ValueError` which the route layer surfaces as 400. `describe_descriptor(operator, descriptor_id)` returns the full `EndpointDescriptor` row joined to its `OperationGroup.group_key`, **omits `embedding`** (384-floats add wire bulk without operator value) but **includes `llm_instructions`** (the per-op agent prompt), tenant-gated (cross-tenant rows collapse to `None` → 404 so the route can't be used as an existence oracle). `api/v1/operations.router` (4 routes mounted at `/api/v1/operations`): `GET /groups` + `GET /search` + `POST /call` are gated on `require_role(OPERATOR)`; `GET /{descriptor_id}` is gated on `require_role(TENANT_ADMIN)` because `llm_instructions` is prompt material. `CallOperationBody` is the Pydantic v2 frozen request body for `POST /call`. `mcp/tools/operations.py` registers three MCP tools (`list_operation_groups`, `search_operations`, `call_operation`) against the G0.5 registry with full JSON-Schema 2020-12 `inputSchema` + `outputSchema` pairs (`additionalProperties: false` on every input), `required_role=OPERATOR`, and load-bearing agent-facing descriptions naming when to call + when NOT to call each tool (the nudge from `call_operation`'s description back to `search_operations` is the recipe scaffold the agent follows). MCP and REST handlers share the meta-tool functions verbatim — wire-shape parity across transports is the contract. `_search.py` carries the SQL + RRF math (kept separate so `meta_tools.py` stays under the code-quality 600-line block). |
| `api/v1/retrieve.router` (`POST /api/v1/retrieve`) | `src/meho_backplane/api/v1/retrieve.py` | Operator-facing diagnostic retrieval surface (G0.4-T5, Task #262). Wraps `meho_backplane.retrieval.retriever.retrieve` with a FastAPI route gated on `require_role(TenantRole.OPERATOR)` -- `read_only` JWTs get 403 + `insufficient_role` log event, `tenant_admin` passes the gate. Request body (`RetrieveRequest`, frozen pydantic v2) validates `query` (min_length=1, max_length=2000), optional `source` / `kind` (max_length=64), `limit` (1-50; matches `retriever.CANDIDATE_LIMIT`). Response (`RetrieveResponse`, frozen) carries `hits: list[RetrievalHit]` + `query_duration_ms` (wall-clock from entry to response build). Tenant-scoped by construction: the route passes `operator.tenant_id` (from the JWT) to `retrieve`; no API surface accepts a tenant id in the body. **Privacy contract**: the audit_log row's `payload` carries `{query_hash, source, kind, hit_count}` -- the SHA-256 hex digest of the raw query (via `_compute_query_hash`, matching the chassis's `compute_body_hash` discipline) + filter metadata + result count. The **raw query string is never persisted** in audit_log (per v0.2 sensitivity defaults / decision #3 in `docs/planning/v0.2-decisions.md`). The route binds the four `audit_*` contextvars before calling `retrieve` so a handler exception still produces an audit row with partial enrichment. MCP resource `meho://retrieve/{query}` deferred to a v0.2.next G0.5 follow-up. |
| `api/v1/retrieve_usage.router` (`GET /api/v1/retrieve/usage`) + `retrieval/usage.py` | `src/meho_backplane/api/v1/retrieve_usage.py` + `src/meho_backplane/retrieval/usage.py` | Audit-backed retrieval usage telemetry (G4.3-T5, Task #444). Reads `audit_log` rows attributable to the five retrieval-class MCP meta-tools (`search_knowledge` / `search_memory` / `search_operations` / `add_to_knowledge` / `add_to_memory`) and returns per-day, per-surface, per-tenant aggregates: `search_count`, `distinct_operators`, `action_conversion_pct` (the share of searches followed by any subsequent audit row from the same operator within `CONVERSION_WINDOW` = 5 min). Three surfaces: kb / memory / operations. Filtering is done via `audit_log.path` matching the MCP tool-call shape (`/mcp/tools/call/<tool>`) — cross-DB-portable without JSON-extraction, indexed by the existing btree on `path`. Successful rows only (`status_code = 200`) — 4xx/5xx attempts are not "daily use". The conversion correlation runs in Python over the two SQL passes (search rows + candidate-action rows in the extended window) — the v0.2 audit_log volume makes in-process correlation cheaper than dialect-specific window-function SQL. `parse_since` accepts `<N>d` / `<N>h` (relative) or ISO-8601 dates; naive datetimes are interpreted as UTC. `UsageReport` + `DailyUsageBucket` are frozen pydantic v2 models. **RBAC**: operator role minimum; the `tenant_filter` query param requires `tenant_admin` (operator + non-null `tenant_filter` → 403 `tenant_filter_requires_tenant_admin`). **Audit + broadcast contract**: the route binds `audit_op_id="meho.retrieval.usage"` + `audit_op_class="audit_query"` contextvars; the chassis publisher honours both as broadcast overrides so the BroadcastEvent emits aggregate-only `{op_class, result_status, row_count}` (per decision #3 in `docs/planning/v0.2-decisions.md` — the audit-query class never broadcasts the request payload). `audit_row_count` reflects `total_searches` (aggregate cardinality), not the raw audit_log scan count. The Go CLI verb `meho retrieval usage` is filed as a follow-up Task (Go toolchain unavailable on the original author's machine); the API ships first and the CLI lands when `make snapshot-openapi` + `make generate` can run. The `cli/api/openapi.json` snapshot in this PR is purely additive (new endpoint + the two response schemas) so the next `make generate` pass produces a clean diff. |
| `_AUDIT_PAYLOAD_PREFIX` / `_resolve_audit_payload` | `src/meho_backplane/audit.py` | Contextvar-driven `audit_log.payload` enrichment (G0.4-T5, Task #262). The audit middleware reads every structlog contextvar whose key starts with `_AUDIT_PAYLOAD_PREFIX` (`"audit_"`), strips the prefix, and merges non-None values into the JSON payload dict before constructing the `AuditLog` row. Routes opt in by `structlog.contextvars.bind_contextvars(audit_query_hash=..., audit_source=...)` inside the handler; the load-bearing namespace discipline (the `audit_` prefix) is what keeps route-specific binding from colliding with the middleware-managed `operator_sub` / `tenant_id` / `request_id` keys. Routes that bind nothing get the chassis-era empty-dict behaviour -- the enrichment is purely additive, no breaking change to existing audit rows. **Broadcast overrides** (G4.3-T5 #444): two reserved keys flowing through this mechanism (`audit_op_id` → `payload["op_id"]`, `audit_op_class` → `payload["op_class"]`) are read by `_publish_broadcast_event` as overrides for the `BroadcastEvent.op_id` / `BroadcastEvent.op_class` fields, so a route can publish under a connector-shaped op_id (`meho.retrieval.usage`) + an explicit `audit_query` class instead of the HTTP-shape default + `classify_op` fallback. Required because `classify_op` would otherwise see only the HTTP-shape op_id (`http.get:/api/v1/retrieve/usage`) and classify it as `other` — which would broadcast the full request payload, defeating the audit-query aggregate-only discipline. The overrides are per-route opt-in; chassis-era surfaces that bind neither key still get the HTTP-shape + `classify_op` behaviour. |
| `retrieve` / `RetrievalHit` / `RRF_K` / `CANDIDATE_LIMIT` | `src/meho_backplane/retrieval/retriever.py` | Hybrid BM25 + cosine retrieval with Reciprocal Rank Fusion (G0.4-T4, Task #261). Shared read path G4 (`meho kb search`), G5 (`meho recall`), and future agent-grounding flows all consume. Two raw-SQL signals run against the indexed `documents` table: BM25 via `ts_rank_cd(to_tsvector('english', body), plainto_tsquery('english', :query))` filtered by `@@` (top 50 by score desc), cosine via `1 - (embedding <=> CAST(:emb AS vector))` (top 50 by distance asc). Each signal returns its top-`CANDIDATE_LIMIT` (50); the in-process `_rrf_fuse` merges by summing `1.0 / (RRF_K + rank)` (rank is 1-based) per document across signals and returns the top-`limit` by fused score. `RRF_K = 60` is the Microsoft 2009 paper default; `CANDIDATE_LIMIT = 50` matches the v0.2 corpus size assumption. The query is embedded once per call via `get_embedding_service().encode_one(query)` -- no per-query LRU in v0.2. Tenant scoping is mandatory (every SQL filters by `tenant_id`); optional `source` / `kind` filters narrow within a tenant via the `CAST(:source AS text) IS NULL OR source = :source` pattern. `RetrievalHit` is a frozen pydantic v2 model carrying `document_id`, `tenant_id`, `source`, `source_id`, `kind`, `body`, `doc_metadata`, plus `fused_score` and per-signal `bm25_score` / `cosine_score` / `bm25_rank` / `cosine_rank` (None when the document didn't appear in that signal's top-50) -- the API surface (T5 #262) returns these unchanged. Empty corpus / no-match query → `[]`, not an error. PG-real coverage (SQL bindings + tenant boundary + filters) lives in T6's `tests/integration/test_retrieval_e2e.py` because the operators have no SQLite analogue. |
| `MemoryService` / `MemoryRbacResolver` / `MemoryScope` / `PermissionDeniedError` | `src/meho_backplane/memory/{__init__.py,service.py,rbac.py,schemas.py}` | Server-side memory layer (G5.1-T1, Task #421). Tenant-scoped wrapper over the G0.4 `documents` table for the five `MemoryScope` values (`user` / `user-tenant` / `user-target` / `tenant` / `target`) from consumer-needs.md §G5 L137-141. Each scope maps to a `documents.kind` of `memory-<scope>`; `documents.source_id` encodes scope-disambiguating context (`user:<user_sub>:<slug>` / `user-tenant:<user_sub>:<slug>` / `user-target:<user_sub>:<target_name>:<slug>` / `tenant:<slug>` / `target:<target_name>:<slug>`) so different scopes never collide on the same slug under the natural-key uniqueness in migration `0003`. `MemoryService.remember/recall/list_memories/forget/search_memories` wrap `index_document` (writes) and `retrieve` (search); the in-process `list_memories` candidate pull uses `kind IN (visible_kinds)` + `order_by updated_at desc` then post-filters in Python for `user_sub` / `slug_pattern` / `tag` / `expires_at` (v0.2 corpora are small per consumer-needs.md L131; SQL-side promotion is the v0.2.next escape hatch). `MemoryRbacResolver` is a stateless matrix: writes for `user-*` scopes allow any non-`read_only` operator (target scopes additionally require `target_name`), `TENANT` requires `tenant_admin`, `TARGET` allows any operator-in-tenant (G0.3 #224's `resolve_target` will tighten the per-target ACL later — call sites are already shaped to consume it). Reads for `user-*` scopes require `operator.sub == stored.user_sub`; `TENANT` / `TARGET` reads are open to every role in the tenant (the substrate's "team becomes the unit of memory" property). `recall` returns `None` on both not-found AND RBAC-denied to avoid 404-vs-403 info-leak; expired entries (stored `expires_at` in `doc_metadata` as ISO 8601 UTC) are filtered out of `recall` / `list_memories` / `search_memories` by default — G5.2 #374's daily executor is what physically reaps them. `MemoryEntry` / `MemoryEntryCreate` / `MemoryEntrySearchHit` are frozen pydantic v2 models the API layer (T2 #422), MCP tools (T3 #423), and CLI verbs (T4 #424) consume unchanged. |
| `index_document` / `compute_body_hash` / `estimate_tokens` | `src/meho_backplane/retrieval/indexer.py` | Canonical write path for the `documents` table (G0.4-T3, Task #260) -- both G4 (#215, kb ingestion) and G5 (#216, memory writes) call this helper rather than re-deriving the hash + embed + upsert sequence. Algorithm: look up by `(tenant_id, source, source_id)`; if the existing row's `body_hash` matches the new body's SHA-256, **skip the embedding compute** and just touch `updated_at` (and `doc_metadata` if the caller passed a new dict) -- this is the cost optimisation that makes `meho kb refresh` against an unchanged corpus essentially free. On body change or first-index, calls `get_embedding_service().encode_one(body)` and either updates in place or inserts a fresh row. The caller passes `tenant_id` explicitly (no contextvar resolution) so the tenant boundary is auditable at the call site; T5's API route extracts from `Operator.tenant_id`. Optional `session` arg: when provided, helper does NOT commit (caller owns the transaction -- batch ingestion shape); when `None`, helper opens its own session via `get_sessionmaker()`, commits, and closes. `metadata=None` preserves existing metadata on the skip-re-embed path; `metadata={}` explicitly clears. `compute_body_hash` is SHA-256-hex of the UTF-8 body (regression-locked against a known hash so the encoding contract can't drift); `estimate_tokens` is `int(len(body.split()) * 1.3)` (v0.2 heuristic; tiktoken deferred). |
| `dispatch` / `import_handler` / `compute_params_hash` / `PassThroughReducer` / `parent_audit_id_var` / `DispatchChild` / `CompositeRecursionLimitExceeded` | `src/meho_backplane/operations/dispatcher.py` (+ `_lookup.py` / `_validate.py` / `_branches.py` / `_handler_resolve.py` / `_audit.py` / `_errors.py` / `reducer.py` / `composite.py`) | The single entry point every MEHO operation routes through (G0.6-T5, Task #396) — the load-bearing centerpiece of Initiative #388. `dispatch(*, operator, connector_id, op_id, target, params) -> OperationResult` orchestrates eight phases per call: (1) parse `connector_id` into `(product, version, impl_id)` via `parse_connector_id` (forgiving — `"vault"` → v1-style `(product="vault", version="", impl_id="")`; `"vmware-rest-9.0"` → `(product="vmware", version="9.0", impl_id="vmware-rest")`), (2) look up the `endpoint_descriptor` row by natural key with tenant-scoped-then-global fallback, (3) validate `params` against `descriptor.parameter_schema` via `jsonschema.Draft202012Validator` (OpenAPI 3.1 compatible), (4) policy gate (v0.2 default-allow except `requires_approval=True` → `denied` so the G10 approval queue can drop in later without re-touching every caller), (5) resolve the connector class via `resolve_connector(target)` and instantiate it (module-level cache keyed on class identity so the per-target `httpx.AsyncClient` / `asyncssh.SSHClientConnection` pool persists), (6) branch on `source_kind`: `ingested` builds a request via `HttpConnector._request_json` / `_post_json` with path-template substitution + `x-meho-param-loc` extension splitting params into path/query/header/body; `typed` resolves the dotted `handler_ref` via `import_handler` (cached importlib walk + getattr chain — handles both module-level functions and bound-class methods, rebinding the latter against the connector instance), `composite` invokes the handler with a `DispatchChild` callable built by G0.6-T7 (#398)'s `get_dispatch_child(...)` — handlers call it as `await dispatch_child(connector_id=, op_id=, params=, target=?)` and the callable owns the audit-tree linkage + bounded-recursion guard internally, (7) JSONFlux-wrap via the module-level `Reducer` Protocol (G0.6-T6, #397) — the dispatcher calls `await reducer.reduce(payload, descriptor.response_schema, context={"op_id", "operator_sub", "audit_id", "source_kind", "target_id"})` and the returned `(summary, handle)` lands on `OperationResult.result` and `OperationResult.handle`; v0.2 default `PassThroughReducer` returns `(payload, None)` verbatim, the real reduction (set-shaped payload heuristics, MinIO/S3 spill, `result_query`/`result_aggregate` meta-tools) ships in a follow-on Initiative, (8) write one `audit_log` row + publish one `BroadcastEvent` via the existing G6.1-T3 hook (fail-open broadcast, fail-loud audit) — the audit row's `parent_audit_id` column (G0.6-T7 #398 migration `0006`) carries the composite parent's id when the row is a recursive child, NULL for top-level dispatches; payload mirrors the same value as a string so the broadcast event surfaces the linkage too. The function **never raises** — every operator-visible failure mode returns a structured `OperationResult(status='error'\|'denied', error="<code>: <detail>", extras={"error_code": ..., ...})` so MCP / CLI / FastAPI callers all see a uniform shape; the codes are `unknown_op` / `invalid_params` / `no_connector` / `handler_unreachable` / `denied` / `connector_error`. `compute_params_hash` is SHA-256 over canonical `json.dumps(params, sort_keys=True, default=str)` — same hash for two dispatches with the same args, audited per-row so retries/composite sub-calls correlate. **G0.6-T7 composite recursion (Task #398)**: `composite.py` ships the `DispatchChild` `typing.Protocol` (the structural callable composite handlers annotate against), `get_dispatch_child(*, dispatch, parent_operator, parent_target, parent_audit_id, parent_op_id) -> DispatchChild` (factory closing over the parent context so handlers don't re-thread it through every sub-call), the `composite_depth_var` contextvar (per-task recursion depth, default 0 for top-level dispatches), and `CompositeRecursionLimitExceeded` (raised pre-recursion when `attempted_depth > Settings.composite_max_depth` (default 8, env `COMPOSITE_MAX_DEPTH`) — the over-depth call writes no audit row, so a runaway composite fails the deepest composite's audit row as `connector_error` and lets the parent decide whether to surface or absorb the failure). The dispatcher's `composite` branch builds the `DispatchChild` once per composite invocation, binds `parent_audit_id_var` + `composite_depth_var` for the duration of each child dispatch (reset via tokens in `finally` so sibling sub-calls see clean state), and passes it to the handler as the `dispatch_child` kwarg. The handler annotates its signature as `dispatch_child: DispatchChild` for static type checking. Recursive-CTE audit-tree queries (G8.1 / G8.2) read `parent_audit_id` directly to reconstruct the full operation tree (composite parent → N children → their own grandchildren). Module is split across eight files (orchestrator + seven concern-keyed helpers in `_lookup` / `_validate` / `_branches` / `_handler_resolve` / `_audit` / `_errors` / `composite` plus the `reducer` protocol) to keep the dispatcher file under the code-quality file-size threshold. |
| `register_typed_operation` / `derive_handler_ref` / `HandlerRefError` | `src/meho_backplane/operations/typed_register.py` | Async upsert helper for the G0.6 operation substrate (G0.6-T4, Task #395). Typed connectors (Vault, K8s, future bind9 / pfSense / Holodeck) call this at init time once per operation they expose; the helper inserts (or updates) one row in `endpoint_descriptor` with `source_kind='typed'`, `tenant_id IS NULL` (built-in / global), `handler_ref` derived from `handler.__module__` + `handler.__qualname__` (the dispatcher T5 imports + `getattr`-walks this dotted path at dispatch time), and `embedding` computed via the shared `EmbeddingService.encode_one` over `summary + description + custom_description + tags`. Idempotency: re-running with **unchanged** `summary` / `description` / `custom_description` / `tags` skips the embedding compute via a SHA-256 hash comparison against the persisted row's recomposed text — operationally load-bearing on connector init, a 50-op connector avoids 50 ONNX inferences per pod restart. Non-embedding fields (`parameter_schema`, `response_schema`, `safety_level`, `requires_approval`, `llm_instructions`, `handler_ref`, `group_id`) still update in place on re-call (with `updated_at` advanced) — only the expensive ONNX call is short-circuited. `group_key` resolves to an existing `operation_group.id` or auto-creates one with `review_status='enabled'` (typed registrations bypass the G0.7 operator-review queue — the typed-connector author already vouched at code-review time). `derive_handler_ref` rejects closures (`<locals>` in qualname), lambdas (`__qualname__ == '<lambda>'`), `functools.partial` wrappers (no `__module__`/`__qualname__`), and non-coroutine functions (`inspect.iscoroutinefunction` is the gate); the rejection raises `HandlerRefError` (a `ValueError` subclass) at registration time so failures surface during lifespan startup rather than at first request. `op_id` is validated as non-empty / non-whitespace; `safety_level` is validated against the `{safe, caution, dangerous}` enum that mirrors the DB CHECK constraint. Caller-owned-session shape mirrors `index_document`: pass `session=` to defer commit (batch registration), omit to let the helper open its own session and commit. `embedding_service=` is the test seam — production callers leave it `None` so the helper resolves the process-wide singleton via `get_embedding_service`. The companion `meho_backplane.operations.embed` module owns the embedding-text composition (`build_embedding_text`) + hash (`compute_embedding_text_hash`, delegating to `compute_body_hash` from the retrieval substrate) — kept separate so a future tweak to "what counts as embeddable surface" lands in one place. |
| `load_corpus` / `KbCorpusQuery` / `MemoryCorpusQuery` / `OperationCorpusQuery` / `CorpusValidationError` | `src/meho_backplane/retrieval/eval/{__init__.py,corpus.py,kb_queries.yaml}` | Eval corpus loader + per-surface Pydantic schemas (G4.3-T1, Task #440). Foundation that T2 (#441 eval runner), T3 (#442 operations corpus), and T4 (#443 memory corpus) all build on. `load_corpus(surface)` reads the per-surface YAML co-located with the module via `importlib.resources` (works in both editable installs and built wheels), parses with `yaml.safe_load`, and validates against a frozen Pydantic v2 schema (`frozen=True, extra="forbid", strict=True`) — frozen prevents in-flight mutation during a multi-pass eval, `extra="forbid"` catches the copy-paste-from-issue-body footgun (e.g. `expected_slug` instead of `expected_hits`), `strict` blocks YAML's bare-yes-becomes-bool footgun. Validation failures raise `CorpusValidationError` naming the surface + filename + Pydantic field-path so an operator running `meho retrieval eval` (T2) can find the bad entry without grepping. T1 ships `kb_queries.yaml` only (10 hand-curated queries against the consumer kb at `claude-rdc-hetzner-dc/kb/`, sourced from operator Slack history + Claude session logs); `load_corpus("memory")` and `load_corpus("operations")` return `[]` until T3 / T4 ship their YAML files so T2's runner can iterate every surface without crashing. The kb corpus is mandatory: a missing `kb_queries.yaml` raises (packaging error). The slug-existence test (`test_kb_corpus_slugs_align_with_consumer_kb_snapshot`) holds a frozenset of consumer kb slugs at corpus-authoring time and fails if the YAML drifts — slug renames update both the snapshot and the corpus in the same PR. |
| `eval_all` / `eval_surface` / `EvalResult` / `SurfaceResult` / `QueryResult` / `Thresholds` / `verdict` / `precision_at_k` / `reciprocal_rank` / `coverage_at_k` / `run_grep_baseline` / `save_baseline` / `load_baseline` / `compare_baseline` | `src/meho_backplane/retrieval/eval/{runner.py,metrics.py,baseline_grep.py,baseline_io.py,result_models.py}` + `src/meho_backplane/api/v1/retrieve_eval.py` + `scripts/ci/run_eval_gate.py` + `.github/workflows/eval-gate.yml` + `ci/eval-baseline.json` | Retrieval-eval runner + CI gate (G4.3-T2, Task #441). The runner is corpus-agnostic: `eval_all(tenant_id, ...)` walks all three surfaces (kb / memory / operations), calls the in-process `retrieve` helper per query (defaults to `meho_backplane.retrieval.retriever.retrieve`; tests inject a stub), folds per-query hits into precision@5 / MRR / coverage via the pure `metrics.py` functions, and returns a frozen Pydantic v2 `EvalResult` whose `overall_verdict` field gates CI. Surfaces with no shipped corpus (memory until T4 #443; operations until T3 #442) return `query_count=0` + `verdict="green"` so the CI gate doesn't flip red on an absent corpus — the retire-checklist verb (T6 #445) is responsible for asserting the corpus actually exists before trusting the green. The threshold contract (`precision@5 >= 0.80 AND MRR >= 0.50 AND coverage >= 0.90` = green; below 70% of any green = red) is encoded in `metrics.verdict()` + `Thresholds`; defaults match the Initiative #373 contract. `--baseline grep` runs `run_grep_baseline()` (asyncio subprocess wrapper around literal-string `-F -i -l --include=*.md` grep), computes the same metrics against grep's hits, and downgrades the surface to red if MEHO loses on any per-metric comparison (the "MEHO ≥ baseline" retire criterion). `save_baseline` / `load_baseline` / `compare_baseline` round-trip an EvalResult through JSON for regression detection — the `--compare-baseline` flag flags any per-metric drop > epsilon (default 0.02). The HTTP route `POST /api/v1/retrieve/eval` is operator-role minimum, tenant-scoped, and binds `audit_op_id="meho.retrieval.eval"` + `audit_op_class="audit_query"` overrides so the broadcast publisher emits aggregate-only events (eval queries can be operator-sensitive). The route accepts a `baseline` field on the request body but rejects any non-empty value with 501 Not Implemented (v0.2 has no server-side corpus snapshot to evaluate against; the CLI runs the baseline locally instead — silent-drop is the worst possible posture so the API is honest about its limits). The Go CLI `meho retrieval eval [--surface kb\|memory\|operations\|all] [--baseline grep] [--save-baseline ...] [--compare-baseline ...] [--json]` lives in `cli/internal/cmd/retrieval/`, exits 1 on red verdict OR baseline regression (CI gate signal), 2 on auth_expired (operator hasn't run `meho login`), 4 on unexpected errors (typo'd `--backplane` URL, corrupt config), and uses direct `net/http` on the existing `AuthedClient.HTTPClient()` / `AccessToken()` / `Refresh()` helpers because the next `make generate` pass to add the typed wrapper for the new endpoint lands in a follow-up PR. The `grep` baseline subprocess is hard-capped at `GREP_TIMEOUT_SECONDS=15.0` via `asyncio.wait_for` + kill-and-drain on timeout so a wedged grep can never hang the FastAPI worker; failure-path structured logs redact the raw query (`query_len` + `query_sha256[:16]` only) to keep the route's audit_query PII posture consistent across audit rows and stdout logs. The CI workflow `.github/workflows/eval-gate.yml` runs `scripts/ci/run_eval_gate.py` on every PR touching `backend/src/meho_backplane/retrieval/**`; the script exercises the runner end-to-end against a deterministic stub retrieve_fn (the "perfect retrieval" reference) and fails the build on red verdict OR baseline regression vs `ci/eval-baseline.json` OR baseline file missing (fail-loud — a missing baseline file makes the gate a no-op, so the script exits 1 with a clear "GATE FAILED: baseline missing" message rather than silently passing). The live BM25+cosine+RRF substrate is NOT exercised by this gate (PG + fastembed model would 5-10x the CI wall clock); that coverage lives in `tests/integration/test_retrieval_e2e.py` + the `chart.yml` `helm-test` job. When T6 #445 wires a per-surface threshold check against the live substrate, this gate becomes the inner ring. |
| `EmbeddingService` / `get_embedding_service` / `EMBEDDING_DIMENSION` | `src/meho_backplane/retrieval/embedding.py` | fastembed-backed in-process embedding pipeline (G0.4-T2, Task #259). `EmbeddingService` wraps `fastembed.TextEmbedding` with lazy model load + `asyncio.to_thread` offload so the event loop stays responsive (ONNX runtime is sync). The fastembed import is local to `_ensure_loaded` so module-import of the retrieval package doesn't pull onnxruntime; `structlog.get_logger()` is also resolved per-call inside the method so a worker-thread call from inside pytest's stdout-captured context doesn't crash with `I/O operation on closed file`. `get_embedding_service()` is the `@lru_cache(maxsize=1)` singleton bound to `Settings.retrieval_embedding_model` + `Settings.retrieval_model_cache_dir`; T3's `index_document` and T4's `retrieve` both route through it. The lifespan in `main.py` calls `encode_one("model preload")` once at startup so the ~1-2 s ONNX load amortises across the pod lifetime; failure is logged warn-level and falls back to lazy-on-first-call. `EMBEDDING_DIMENSION = 384` is the load-bearing contract — must match the `vector(384)` column type in migration `0003`; a future model with different dimensionality requires a re-embed-everything migration. |
| `tests/test-pgvector.yaml` (Helm test) + chart.yml `helm-test` job | `deploy/charts/meho/templates/tests/test-pgvector.yaml` + `.github/workflows/chart.yml` | G0.4-T6 (#263) chart-side preflight. Helm test Pod (`helm.sh/hook: test`) runs `bitnami/postgresql:16` with `psql` to assert the deployed Postgres has the `vector` extension enabled (`SELECT extversion FROM pg_extension WHERE extname='vector'`) and the `documents` table is reachable (`SELECT count(*) FROM documents`). Both assertions emit clear FAIL messages on miss so the operator's `helm test --logs` output points at the exact drift. The `chart.yml` workflow's `helm-test` job spins up a kind cluster, installs `pgvector/pgvector:pg16` as an in-cluster Service, helm-installs the chart with `--wait-for-jobs` (waits for the migration Job, not Pods -- the backplane's readiness probe needs real Keycloak / Vault which we don't mock here), then runs `helm test`. Marked `continue-on-error: true` as the v0.2 transitional posture; the follow-up issue hardens the workflow before removing the bypass. The integration test `tests/integration/test_retrieval_e2e.py` covers the same surface from the Python side end-to-end. |
| `retrieval.modelCache` chart values | `deploy/charts/meho/values.yaml` + `deploy/charts/meho/values.schema.json` + `deploy/charts/meho/templates/pvc-fastembed-cache.yaml` + `deploy/charts/meho/templates/deployment.yaml` | G0.4-T2 (#259) chart-side surface. `retrieval.modelCache.enabled` (default `true`) renders a `ReadWriteOnce` PVC named `<release>-fastembed-cache` of size `retrieval.modelCache.size` (default `200Mi` — enough headroom for `BAAI/bge-small-en-v1.5`'s ~120 MB), mounted at `retrieval.modelCache.mountPath` (default `/var/cache/fastembed`) inside the backplane Pod. The ConfigMap binds `RETRIEVAL_EMBEDDING_MODEL` + `RETRIEVAL_MODEL_CACHE_DIR` from the matching `config.retrievalEmbeddingModel` / `config.retrievalModelCacheDir` keys (default `BAAI/bge-small-en-v1.5` + `/var/cache/fastembed`); operators that need a multi-replica RWX cache provide their own PVC via `extraVolumes` + `extraVolumeMounts` and either flip the chart-managed PVC off or accept the unused volume. The migration Job deliberately does **not** mount the cache — migrations don't embed. |
| `HealthResponse` / `OperatorIdentity` / `VaultStatus` / `DbStatus` | `src/meho_backplane/api/v1/health.py` | Frozen pydantic v2 response models. `OperatorIdentity` deliberately excludes `raw_jwt` so the bearer token never appears in the response body. `DbStatus.migrated` reflects the T27 DB-migration-state probe verdict (true when current matches Alembic head, false otherwise; `bool \| None` is preserved for forward compatibility with chassis-stage decoders). `VaultStatus.detail` carries only structured tokens (`version=N`, `read_failed: <ExcClass>`, `login_failed: <ExcClass>`) — no operator-controllable URL substrings. |
| `_no_secret_leak_sweep` | `tests/conftest.py` (autouse) | Pytest fixture that runs after every test in `tests/`, scanning `capfd`-captured stdout/stderr and `caplog` records for credential-shaped substrings (`Bearer <long>`, `password=`, `secret=`, `token=`, `api_key=`, `Authorization: Bearer …`). First match → `pytest.fail` with a redacted preview. The patterns live in `SECRET_LEAK_PATTERNS` for contributor extension; the targeted leak tests in `tests/test_secret_leak_checks.py` complement the always-on sweep with explicit assertions on the structlog `StringIO` buffers used by route-level tests. |
| `ReviewService` / `ConnectorReviewPayload` / `parse_connector_id` / `ConnectorNotFoundError` / `InvalidStateTransitionError` | `src/meho_backplane/operations/ingest/` (`service.py` + `_internals.py` + `parser.py` + `payload.py` + `exceptions.py`) | G0.7-T4 (Task #402) review-queue state machine. Gates ingested connectors through `staged → enabled → disabled` transitions before any operation reaches the agent surface. Five public mutating methods on `ReviewService` — `enable_connector` / `disable_connector` / `enable_group` / `edit_group` / `edit_op` — plus one read method (`get_review_payload`). Every state-mutating call writes exactly one `audit_log` row with `method='SERVICE'`, `path` ∈ {`meho.connector.enable`, `meho.connector.disable`, `meho.connector.enable_group`, `meho.connector.edit_group`, `meho.connector.edit_op`}, in the same transaction as the state mutation (commit-together-or-not-at-all). Idempotent re-invocations are pure no-ops (no rows change → no audit row). The `is_enabled` cascade on `enable_connector` consults the audit log to find ops the operator explicitly set to `is_enabled=False` via `edit_op` and skips them — operator overrides survive subsequent connector-level enables. `disable_connector`'s cascade is blanket (no override consultation) because connector-level disable is a regression rollback and overrides per-op intent for the disabled duration. Authorisation: the service is constructed with an `Operator`; every method takes an explicit `tenant_id: UUID \| None`. `tenant_id == operator.tenant_id` is always allowed; `tenant_id is None` (built-in scope) requires `TenantRole.TENANT_ADMIN`; cross-tenant access uniformly raises `ConnectorNotFoundError` so probe-by-status-code attacks surface no information. `parse_connector_id("vmware-rest-9.0")` → `("vmware", "9.0", "vmware-rest")` via the "first dash before a digit" convention from `docs/architecture/connectors.md`; ambiguous multi-dash versions (`hetzner-robot-2026-04`) round-trip correctly. The public class signatures take `tenant_id` as a keyword-only arg on every method (a slight refinement of the Task body's API, which omitted it from some methods) so built-in vs tenant-curated scopes have uniform call shape. Audit attribution echoes the operator's tenant on the row, not the target scope — a `tenant_admin` editing built-in (NULL) rows still gets `audit_log.tenant_id` populated. Out of scope for T4: CLI verbs (T5 #405), REST routes (T6 #406), admin MCP tools (T7 #407); all three layer over this service. |
| `KbService` / `KbEntry` / `KbEntrySearchHit` / `KbIngestionResult` / `validate_slug` / `KB_SOURCE` / `KB_KIND_ENTRY` / `SLUG_PATTERN` / `InvalidKbSlug` | `src/meho_backplane/kb/schemas.py` + `src/meho_backplane/kb/service.py` + `src/meho_backplane/kb/file_walker.py` | G4.1-T1 (#415) tenant-scoped knowledge-base service over the G0.4 `documents` substrate. `KbService` exposes six async methods callers route through: `ingest_directory(directory, tenant_id, dry_run)` walks a kb directory and ingests every `.md` file via `index_document(source="kb", kind="kb-entry", source_id=<slug>)`, returning a four-bucket `KbIngestionResult` (`inserted_count` / `updated_count` / `skipped_count` / `error_count` + per-file `errors` list). Per-file failures (binary masquerading as `.md`, invalid slug, malformed front-matter) are caught + counted + appended to `errors`; the run continues with remaining files. `list_entries(tenant_id, filter_pattern, limit, offset)` returns slug-sorted entries with optional SQL `LIKE` pattern narrowing — pure list, no retrieval. `get_entry(tenant_id, slug)` returns the full body or `None`. `create_entry(tenant_id, slug, body, metadata)` validates the slug then delegates to `index_document` (body-hash short-circuit applies). `delete_entry(tenant_id, slug)` returns a bool indicating whether a row existed. `search_entries(tenant_id, query, filters, limit)` wraps `retrieve(source="kb")` and adapts `RetrievalHit` → `KbEntrySearchHit` (renames `source_id` → `slug`, truncates body to a 200-char snippet ending in `…`). RBAC is **not** enforced by the service — the route layer (T2 #416) owns the `require_role(TenantRole.TENANT_ADMIN)` gate for write ops. **Slug regex** (`SLUG_PATTERN` in `schemas.py`): `^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$` — starts with a lowercase ASCII letter, ends with a lowercase letter or digit, middle is lowercase letters / digits / hyphens / **dots**. The dotted middle is load-bearing for the consumer kb's version-numbered filenames (e.g. `vcenter-9.0-snapshot-revert.md` → slug `vcenter-9.0-snapshot-revert`); the task body's example regex `^[a-z][a-z0-9-]*$` excluded dots, this implementation relaxes to honour the example. `validate_slug` raises `InvalidKbSlug` (subclass of `ValueError`) so callers can `except ValueError` without importing the kb module. **File walker** (`file_walker.py`): `walk_kb_directory(root, errors=None)` yields `KbFileRecord(path, slug, body, metadata)`; strict mode (`errors=None`) propagates per-file exceptions, best-effort mode (`errors=[]`) catches read / parse / slug failures and appends to the supplied list (the generator-recovery pattern: a per-file catch must live *inside* the generator's loop because Python generators close on internal exception propagation — `KbService.ingest_directory` relies on this). Hidden paths (any component starting with `.`) are always skipped. Optional root-level `.kb-ignore` file: one glob pattern per line, `#` comments + blank lines ignored, patterns matched against both the full POSIX-relative path and each path component (so `drafts` skips every file under `drafts/`). Front-matter parsing uses `python-frontmatter` 1.1.0 (`frontmatter.loads()` → `Post.metadata` dict + `Post.content` body string); malformed YAML raises `KbFileParseError` chained from `yaml.YAMLError`. Slug extraction prefers a non-empty string `slug:` front-matter override over the filename stem (`Path.stem`); the consumer kb has no front-matter today, the override is future-compat. PG-real coverage in `tests/integration/test_kb_service_pg.py` exercises idempotent ingestion (10 inserts → 10 skips), search top-3 ranking of freshly-created entries, and the tenant boundary across `list_entries` + `search_entries`. Sibling waves under Initiative #331 build on this service: T2 #416 REST routes, T3 #417 MCP meta-tools (`search_knowledge` + `add_to_knowledge`), T4 #418 CLI verbs, T5 #419 canary acceptance, T6 #420 cross-repo runbook. |
| `tests/integration/test_tenant_isolation.py` | `backend/tests/integration/test_tenant_isolation.py` | G0.1-T6 (Task #236) broad-spectrum end-to-end test for the tenancy chain. Boots `pgvector/pgvector:pg16` via `testcontainers` (module-scoped fixture in `tests/integration/conftest.py`; image name env-overridable via `MEHO_TEST_PGVECTOR_IMAGE`), applies `alembic upgrade head` against the asyncpg URL, builds a fresh `FastAPI` with the production middleware stack plus the `/api/v1/rbac-test` stub routes mounted unconditionally, and exercises five integration cases: (1) two operators in two tenants generate correctly tenant-scoped audit rows (5+3 split, no cross-pollination); (2) a JWT signed with an unknown tenant_id still authenticates and lands its row under the bogus UUID — documents v0.2's "trust the issuer's claim" model; (3) the per-tenant query helper returns only the requesting tenant's rows (forward-compat for G8); (4) a `read_only` JWT on `/api/v1/rbac-test/operator` returns 403 — sanity for T4's RBAC primitive; (5) the highest-value test — eight interleaved requests under `asyncio.gather` from four operators across two tenants must produce audit rows attributed to the right tenant_id, catching structlog contextvar leaks across concurrent asyncio tasks. The `_skip_no_docker` class-level mark mirrors `tests/test_migration_rollback.py`'s pattern: agent sandboxes without Docker skip the PG-driven tests; CI runners with Docker provisioned run them. The cheap import-smoke test `test_module_imports_cleanly` runs unconditionally so a renamed fixture surfaces at collection time. |

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
     contextvars on success), dispatches `vault.kv.read` via the
     connector registry (`VaultConnector.execute`), reads
     `secret/meho/test/federation`, and returns the `HealthResponse`
     document. The middleware's eventual `request_completed` log line
     inherits `operator_sub` because the binding lives in the same
     request-scoped contextvar context.

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
| `pgvector` | ≥ 0.3, < 1.0 | Postgres vector extension Python adapter (G0.4-T1 #258). Provides `pgvector.sqlalchemy.Vector` used by `Document.embedding` on PG; the SQLite test path falls back to a JSON-encoded `Text` via the in-tree `_PortableVector384` TypeDecorator. No `py.typed` marker as of 0.4 — `[tool.mypy.overrides]` whitelists `pgvector.*`. |
| `fastembed` | ≥ 0.7, < 1.0 | In-process ONNX embedding pipeline (G0.4-T2 #259). Ships its own bundled ONNX runtime + tokenizers; no PyTorch dependency. The backplane uses `fastembed.TextEmbedding` exclusively (one class surface), wrapped by `meho_backplane.retrieval.embedding.EmbeddingService`. Default model `BAAI/bge-small-en-v1.5` (384-dim, Apache-2.0, ~120 MB ONNX weights). No `py.typed` marker as of 0.8 — `[tool.mypy.overrides]` whitelists `fastembed.*`. |
| `python-frontmatter` | ≥ 1.1 | YAML front-matter parser for kb file walker (G4.1-T1 #415). Used by `meho_backplane.kb.file_walker._build_record` via `frontmatter.loads(text)` → `Post.metadata` dict + `Post.content` body. Pure-Python (pulls `PyYAML` transitively); MIT-licensed. No `py.typed` marker as of 1.1.0 — `[tool.mypy.overrides]` whitelists `frontmatter.*`. Files without front-matter return `metadata == {}` and `content == <original text>`; malformed YAML raises `yaml.YAMLError` which the walker wraps as `KbFileParseError`. |
| (dev) `aiosqlite` | ≥ 0.19 | Async SQLite driver used for local-dev / test DBs that do not need Docker. The probe + engine module both work against `sqlite+aiosqlite://` URLs because the driver-specific surface is encapsulated by SQLAlchemy. |
| (dev) `testcontainers` | ≥ 4.0 | Spins up `pgvector/pgvector:pg16` for the testcontainer suites (`tests/test_db_engine.py::TestPostgresIntegration`, `tests/test_migration_rollback.py`, `tests/integration/test_tenant_isolation.py`); image name overridable via `MEHO_TEST_PGVECTOR_IMAGE`. Skipped gracefully when the Docker socket is absent — the SQLite-async coverage stays always-on. |
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

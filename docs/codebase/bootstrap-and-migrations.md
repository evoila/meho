<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Bootstrap and Database Migrations

> **Operator and contributor entry points** are the runbooks, not this document. Read those first if you don't already know the system:
>
> - [docs/getting-started.md](../getting-started.md) -- 15-minute onboarding walkthrough with troubleshooting.
> - [docs/deployment.md](../deployment.md) -- full deployment runbook (env vars, ports, rescue script, production hardening).
> - [docs/contributing/migrations.md](../contributing/migrations.md) -- one-page migration authoring guide.
>
> This page is the **archaeology**: how the bootstrap path actually works under the hood, where each piece of code lives, and why specific decisions were made. Keep reading if you're modifying the bootstrap, debugging a migration runner, or onboarding a new system into the lifespan.

## Overview

This document describes how MEHO.X boots from a clean clone to a running application, and how the database schema is managed across that boot path. It covers the local developer loop, the containerized loop, the test loop, and the production deployment loop.

The area was consolidated as part of [Goal #294](https://github.com/evoila-bosnia/MEHO.X/issues/294) from a prior per-module Alembic layout into a single unified migration tree. Most of the "fossil" warnings in this document have been resolved; the residual references are kept for context so engineers reading old branches or commits can correlate what they see.

## Scope of this document

**In scope:**

- Database migration machinery (Alembic configuration, migration runner scripts, squash history)
- Database engine and session wiring (`meho_app/database.py`, `meho_app/core/config.py`)
- Local and containerized bootstrap paths (`scripts/dev-env.sh`, `docker/docker-entrypoint.sh`, Dockerfiles, compose files)
- Application startup lifecycle where it depends on the schema being ready (`meho_app/main.py` `lifespan`)
- Integration test schema setup (`tests/integration/conftest.py`)

**Out of scope:**

- Frontend bootstrap (`meho_frontend/`)
- Keycloak realm configuration beyond the `init-db.sql` database creation
- Business logic inside `lifespan()` beyond its interaction with schema readiness
- CI pipeline beyond the gap where a bootstrap smoke test belongs

## The nine boot stages

A clean-clone run of MEHO.X goes through nine stages. Each stage has a single job; failure in any one of them manifests as "the app won't start" or "the app starts but crashes on first query."

| # | Stage | Responsibility | Owned by |
|---|---|---|---|
| 1 | Environment file present | `.env` exists with required variables, valid Fernet `CREDENTIAL_ENCRYPTION_KEY` | Developer (manual copy of `env.example`) |
| 2 | Images built | Backend image (`Dockerfile.meho`) compiled, Python deps installed | `docker compose build` |
| 3 | Infrastructure started | postgres, redis, minio, keycloak (and optionally seq) | `docker compose up -d` |
| 4 | First-boot SQL | `keycloak` and `keycloak_test` databases created inside postgres | `scripts/init-db.sql` mounted at `/docker-entrypoint-initdb.d/` (runs once per volume) |
| 5 | Health gates | `pg_isready`, `redis-cli ping`, keycloak `/health/ready` all return OK | Compose healthchecks |
| 6 | Migrations applied | All schema changes materialized in postgres | `scripts/run-migrations-monolith.sh` → per-module `alembic upgrade head` |
| 7 | Keycloak configured | Realm, clients, seed tenants created | `scripts/setup-keycloak.sh` |
| 8 | Application lifespan | Topology processor, connector ops sync, skill seeding, stuck-job cleanup, knowledge chunk reconciliation | `meho_app.main:lifespan` |
| 9 | Frontend ready | `meho-frontend` container up, pointing at backend and keycloak | `docker compose up meho-frontend` |

### Invocation paths

All compose-based invocations build the unified `docker/Dockerfile.meho`. Compose now uses a base + override layout (consolidated under #304):

- `docker-compose.yml` — authoritative base, builds `target: prod` and exposes only application ports.
- `docker-compose.override.yml` — auto-loaded development overlay; switches the meho service to `target: debug` and exposes debugpy on port 5678.
- `docker-compose.test.yml` — explicit test overlay (`tmpfs` postgres/minio, separate `meho_test` and `keycloak_test` databases, application services disabled).

Both build targets inherit the shared `ENTRYPOINT ["/docker-entrypoint.sh"]` defined in the `base` stage, so migrations run on every compose-driven boot regardless of target:

- **`docker compose up` (no extra flags)** — auto-merges base + `docker-compose.override.yml`, builds `target: debug`, exposes debugpy on 5678, mounts the source tree for live reload. This is the default developer experience.
- **`docker compose -f docker-compose.yml up`** — opts out of the override and builds `target: prod` (slimmer image, no debugger). Use for local production-image smoke tests.
- **`docker compose -f docker-compose.yml -f docker-compose.test.yml up -d`** — boots the tmpfs test infrastructure used by `tests/integration/`. Application services are scoped to a `disabled` profile and do not start.
- **`./scripts/dev-env.sh up`** — delegates to `docker compose up -d` (which now picks up the override automatically) and runs `run_migrations()` idempotently via `docker compose exec`. Because the entrypoint already ran migrations, the wrapper's second call is a no-op under normal conditions; it remains as a safety net for the `local` path below.
- **`./scripts/dev-env.sh local`** — starts infrastructure in docker, runs the backend and frontend on the host machine via `uv run`, and calls `run_migrations local`, which runs `alembic` against `localhost:5432`. This is the only path that bypasses the docker entrypoint, so the bash-level migration invocation stays load-bearing for host-side development.

## Database topology

MEHO.X uses **one PostgreSQL instance** (the `pgvector/pgvector:pg15` image) hosting three logical databases:

- `meho` — the application database (owned by the user `meho`, populated by migrations)
- `keycloak` — Keycloak's own schema, managed by Keycloak
- `keycloak_test` — test variant, used by `docker-compose.test.yml`

The `keycloak` and `keycloak_test` databases are created by `scripts/init-db.sql`, which postgres runs **once** on the first boot of a fresh `postgres_data` volume (via the image's `/docker-entrypoint-initdb.d/` convention). Subsequent container restarts do not re-run the script, which is the correct behavior but means that destroying and recreating the `postgres_data` volume is the only way to get these databases back if they're lost.

The application connects via `DATABASE_URL`, which is a Pydantic-required field in `meho_app/core/config.py` and has no default — startup fails fast if it is missing. The URL must use the `postgresql+asyncpg://` driver because MEHO.X is async-throughout; a plain `postgresql://` URL will crash on engine creation.

Connection pooling is configured in `meho_app/database.py`:

- `pool_size=10`, `max_overflow=20` — up to 30 concurrent sessions per process
- `pool_pre_ping=True` — validates each connection before handing it out, cheap protection against postgres recycling idle connections
- `pool_recycle=600` — proactively recycles connections every 10 minutes

The engine and session maker are **lazy module-level singletons** (`_engine`, `_session_maker`), initialized on first call to `get_engine()` / `get_session_maker()`. This means nothing happens at import time — good for testability, but also means configuration errors only surface when someone actually asks for a session.

## The Alembic layout

MEHO.X has **one** Alembic tree at `meho_app/alembic/` and **one** `alembic_version` table in the `meho` database. Every module shares the same `meho_app.database.Base` registry, so the unified `target_metadata = Base.metadata` covers the whole application — autogenerate sees one consistent schema slice, not a per-module fragment.

```
meho_app/alembic/
├── alembic.ini              # at meho_app/alembic.ini (sibling of the alembic/ dir)
├── env.py                   # imports every module's models, sets target_metadata = Base.metadata
├── script.py.mako
└── versions/
    ├── 0001_init.py         # consolidated initial schema
    ├── 0002_dedup_skills.py
    ├── 0003_rm_delegate_cred.py
    ├── 0004_webhook_to_event.py
    ├── 0005_webhook_secret.py
    ├── 0006_jobs_scope.py
    ├── 0007_jobs_resume.py
    ├── 0008_jobs_summary.py
    └── 0009_doc_family.py
```

Three properties follow from this shape:

**1. One metadata registry, one autogenerate target.** `meho_app/alembic/env.py` imports every module's `models.py` and sets `target_metadata = Base.metadata`. Autogenerate compares the live database against the whole application schema in one pass; there is no longer a possibility for unimported modules to look "missing" and produce spurious `DROP TABLE` statements.

**2. Linear history.** `down_revision` chains express ordering; there is no shell-side topological sort. Adding a module means adding a `Models.py` import to `env.py` (so its tables enter `target_metadata`) and writing one new revision file pointing at the current head — no shell-array edits, no per-module config files.

**3. One version-tracking table.** Operators check schema state with `SELECT version_num FROM alembic_version` once, not nine times. The legacy `alembic_version_meho_*` tables are gone; their bookkeeping was migrated by `scripts/migrate_to_unified_alembic.py` (#300) for existing deployments.

`0001_init.py` is the consolidated initial schema, hand-written from the previous nine `0001_squash.py` files in foreign-key dependency order so a fresh `alembic upgrade head` produces the canonical state. Revisions 0002–0009 are the real incremental history that landed since the squash. The unified tree currently reports a small acknowledged drift between `0001_init` and `Base.metadata` for FK names, model-side `index=True` declarations the squash never emitted, and HNSW/GIN indexes declared as raw DDL but not on the SQLAlchemy side; reconciling that residue is tracked separately in #466.

## Migration runner

### `scripts/run-migrations-monolith.sh`

A thin wrapper around `uv run alembic -c meho_app/alembic.ini upgrade head`. The script does two things and only two:

1. Refuses to run if a stale per-module `alembic/` directory is still on disk (legacy artifact from before #299). Errors loud with the offending paths and points at the rescue script (#465 covers the rescue-script `--clean-host-paths` follow-up).
2. Runs `alembic upgrade head` against the unified tree. Failures propagate; there is no `2>/dev/null` and no `|| true`.

Called from:

- `docker/docker-entrypoint.sh` (every backend container, both `prod` and `debug` targets via the shared base stage).
- `meho-dev local` (host-side migration trigger when uvicorn runs outside docker; see "Bootstrap wrappers" below).

The dockerized `meho-dev up` path does **not** call this runner a second time — the entrypoint has already migrated, and the lifespan schema gate (`_ensure_schema_ready`, #313) refuses to let the meho container become healthy until `alembic_version` matches head. Once `meho-dev up`'s `_wait_for_service("meho")` returns, migrations have necessarily already succeeded.

### Bootstrap wrappers

`scripts/dev-env.sh` is a 21-line shim that delegates to `meho-dev` (a Typer CLI shipped with the wheel as `meho_app.tools.dev:app`, #310). The two-function bash split (`run_migrations` + `run_migrations_local`) and its `2>/dev/null || true` silent-failure path are gone — `meho-dev`'s `_run_migrations(mode="docker"|"local")` is one tested function with explicit exit-code propagation.

Stale legacy scripts (`reset-db.sh`, `migrate-down.sh`, `migrate_knowledge_alembic_version.py`, `check-migrations.sh`, `stamp-squash.sh`) were removed in #306 / #310. Their roles are subsumed by the unified Alembic tree (#299), the schema-readiness gate (#313), and the rescue script (`scripts/migrate_to_unified_alembic.py`, #300).

## Docker image layout

A single Dockerfile at `docker/Dockerfile.meho` builds every backend image via multi-stage targets:

- `base` — shared stage. Installs apt deps, drops in the `uv` static binary, runs `uv sync --frozen --no-install-project` for optimal layer caching, copies the project source, runs `uv sync --frozen` to finalize the project install, and declares the shared `ENTRYPOINT ["/docker-entrypoint.sh"]`. The image is CPU-only and single-container by design — embeddings run in-process via fastembed (ONNX); document conversion uses the lightweight `pymupdf4llm` + `pdfplumber` pipeline.
- `prod` — production image. Inherits `base`. `CMD` runs uvicorn directly. Defines a `HEALTHCHECK` that polls `/health`.
- `debug` — development image. Inherits `base`. Adds `uv sync --frozen --group dev` for ruff/mypy/pytest-watch/ipython/debugpy. `CMD` wraps uvicorn in debugpy on port 5678 with `--reload` against the volume-mounted source tree. No `--no-editable`, so edits land immediately.

The frontend has its own Dockerfile (`docker/Dockerfile.meho-frontend`) which has no database touchpoint and is not discussed here.

## Compose file layout

Three compose files exist, in a base + override layout (consolidated under #304):

- `docker-compose.yml` — the authoritative base for every environment. Defines all services (postgres, redis-stack-server, minio, keycloak, seq, meho, meho-frontend), mounts `init-db.sql`, and uses Compose's required-variable syntax for `CREDENTIAL_ENCRYPTION_KEY` (`${VAR:?...}`). Builds `docker/Dockerfile.meho` with `target: prod`. Single source of truth.
- `docker-compose.override.yml` — auto-loaded development overlay (replaces the pre-#304 standalone `docker-compose.debug.yml`). About 10 lines: switches `target` from `prod` to `debug` and adds the 5678 debugpy port. Everything else is inherited from the base. Run `docker compose -f docker-compose.yml up` to opt out of it.
- `docker-compose.test.yml` — explicit test overlay (paired with the base via `docker compose -f docker-compose.yml -f docker-compose.test.yml`). Adds `tmpfs` for postgres and minio, switches the keycloak DB to `keycloak_test`, the postgres DB to `meho_test`, points keycloak at the `tests/fixtures/keycloak` realm, and disables `meho`/`meho-frontend`/`seq` via the `disabled` profile so pytest can run application code directly against the infrastructure.

Compose's native base + override composition (`docker compose -f base.yml -f override.yml`) is now the only supported invocation style. The previous warning ("DO NOT USE docker compose DIRECTLY!") is obsolete and has been removed.

## Application startup (`lifespan`)

When the backend container starts, `uvicorn meho_app.main:app` runs the `lifespan` async context manager (defined in `meho_app/main.py`). The lifespan is split into three named phases (#314), each owned by a small async function:

1. **`_ensure_schema_ready()` — Phase 0, fatal.** Compares the database's current `alembic_version` to the latest head from the script directory. On mismatch, raises `SystemExit` with a single log line that names both revisions and the three commands an operator can run to recover (`alembic -c meho_app/alembic.ini upgrade head` directly, the same inside the container, or `./scripts/dev-env.sh up` for a full bootstrap). The container never accepts HTTP traffic until the schema check passes.

2. **`_start_background_workers()` — Phase 1, non-fatal.** Starts long-running workers gated by feature flags: the topology auto-discovery batch processor (Redis-backed), the APScheduler-based scheduled-tasks scheduler with the audit-purge job (enterprise only), and the Slack bot (Socket Mode). Each subsystem owns its own narrow `try/except` with a `Phase 1 --` log prefix, and the function returns a handle dict the shutdown phase uses to stop them cleanly.

3. **`_seed_initial_data()` — Phase 2, non-fatal.** Idempotent setup: cleans up any ingestion jobs left in a stuck state by a previous crash (#90.2), syncs typed connector operations into the knowledge base, runs the chunk reconciler, and ensures baseline orchestrator skills exist for every tenant. Each step is independent; one bad seeder must not abort the others.

The shutdown half of the lifespan reverses Phase 1 in handle order (processor → scheduler → Slack bot → Redis) with `Shutdown --`-prefixed log lines and narrow exception types from #287 (`(RuntimeError, OSError)`, `(RedisConnectionError, RedisTimeoutError, ConnectionError, OSError)`, etc.).

## Integration test schema setup

`tests/integration/conftest.py` applies the unified Alembic tree against the test database via `alembic.command.upgrade(cfg, "head")` once per pytest session, then uses `TRUNCATE … RESTART IDENTITY CASCADE` on every non-`alembic_version` table for per-test isolation:

```python
@pytest.fixture(scope="session")
def _migrate_test_database() -> None:
    _run_alembic_upgrade(_test_database_url())  # alembic.command.upgrade(cfg, "head")

@pytest.fixture
async def db_session(_migrate_test_database) -> AsyncGenerator:
    engine = create_async_engine(_test_database_url(), echo=False, poolclass=NullPool)
    try:
        ...
        yield session
        await _truncate_all_tables(engine)
    finally:
        await engine.dispose()
```

Three properties:

1. **Tests build the schema from migrations, identical to production.** A broken migration aborts the test suite at session setup with the Alembic traceback intact; the suite cannot pass against a half-built schema.
2. **Per-test isolation is fast.** `TRUNCATE … RESTART IDENTITY CASCADE` follows foreign-key chains so tests do not need to know dependency order, and resets sequences. Wall-clock cost is roughly 10ms per test against a typical dev laptop, vs. ~2s for `DROP/CREATE`.
3. **The session migration is not `autouse`.** Bootstrap-style integration tests (e.g. `tests/integration/test_first_run_arm64.py`) intentionally run *before* a stack is up. They bring up their own Compose project; an autouse Alembic upgrade against `localhost:5432` would fail at session setup before those tests' own fixtures got a chance to run. Tests that need the schema declare `db_session` (which lists `_migrate_test_database` as a positional dependency); tests that own their stack are unaffected.

The "every-PR `docker compose up` + `/health`" CI smoke job is tracked separately in #464 — Initiative #298 owns the criterion but the implementation lives in that follow-up.

## Authoring a new migration

The current loop for adding a schema change:

1. Edit the model in `meho_app/modules/<area>/models.py`.
2. Boot the test stack so an empty `meho` database is reachable on `localhost:5432`:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.test.yml up -d postgres
   ```
3. Generate the revision:
   ```bash
   uv run alembic -c meho_app/alembic.ini revision --autogenerate -m "<imperative description>"
   ```
4. Review the generated `meho_app/alembic/versions/00NN_<slug>.py`. Autogenerate is reliable now (one tree, one metadata) but still produces noise for the residual model-vs-migration drift tracked in #466 — read the file end to end before committing.
5. Apply locally:
   ```bash
   uv run alembic -c meho_app/alembic.ini upgrade head
   ```
6. Run the integration suite (the session fixture replays your new migration on a fresh test DB; a broken migration fails the suite at setup).
7. Commit. The next colleague who pulls the branch picks up the migration via the entrypoint when they run `docker compose up`.

A one-page authoring guide with the same flow lives at [docs/contributing/migrations.md](../contributing/migrations.md).

## Key files

| Concern | File | Role |
|---|---|---|
| Engine + session wiring | `meho_app/database.py` | Lazy global async engine and session maker; shared `Base` |
| App configuration | `meho_app/core/config.py` | Pydantic Settings; `DATABASE_URL` required, no default |
| App startup | `meho_app/main.py` `lifespan` | Three-phase: `_ensure_schema_ready` (fatal) → `_start_background_workers` → `_seed_initial_data` |
| Unified Alembic tree | `meho_app/alembic/` | One `env.py`, one `versions/`, one linear history |
| Alembic config | `meho_app/alembic.ini` | Single config consumed by every invocation path |
| Migration runner | `scripts/run-migrations-monolith.sh` | Stale-dir guard + `alembic upgrade head`; no shell-side ordering |
| One-time rescue | `scripts/migrate_to_unified_alembic.py` | Operator-driven; stamps unified `alembic_version` from legacy nine-table state |
| Container entrypoint | `docker/docker-entrypoint.sh` | Runs migrations then `exec`s the image CMD |
| Backend image (unified) | `docker/Dockerfile.meho` | Multi-stage; `prod` and `debug` targets share `ENTRYPOINT` |
| Base compose | `docker-compose.yml` | Authoritative stack; builds `target: prod` |
| Dev override | `docker-compose.override.yml` | Auto-loaded by `docker compose up`; switches to `target: debug`, exposes debugpy on 5678 |
| Test override | `docker-compose.test.yml` | tmpfs postgres/minio; `meho_test`/`keycloak_test` DBs; app services disabled |
| Bootstrap wrapper (CLI) | `meho_app/tools/dev.py` (`meho-dev`) | Typer app shipped with the wheel; subcommands `up`, `down`, `restart`, `local`, `logs`, `status`, `validate`, `test`, `test-all` |
| Bootstrap wrapper (shim) | `scripts/dev-env.sh` | 21-line bash compatibility shim that delegates to `meho-dev` |
| `env.example` sync | `scripts/check-env-example-sync.py` | Pre-commit + `make verify` gate; cross-references Pydantic Settings against `env.example` |
| Postgres init | `scripts/init-db.sql` | Creates `keycloak` and `keycloak_test` databases (first boot only) |
| Integration fixtures | `tests/integration/conftest.py` | Alembic-driven session setup, per-test `TRUNCATE … RESTART IDENTITY CASCADE` |

## What changed in Goal #294

The pre-Goal state was a fossil of an abandoned distributed-services consolidation: nine per-module Alembic trees over one shared database, three divergent bootstrap paths that disagreed with each other (`docker compose up` plain, `docker-compose.debug.yml` with no entrypoint, a 580-line `dev-env.sh` wrapper), error suppression that hid migration failures (`run_migrations_local` reported success regardless of outcome), and an integration test suite that bypassed Alembic entirely so broken migrations could merge with a green CI. PR #293 had triaged the immediate symptoms; Goal #294 closed the structural causes.

| Theme | Before | After | Issues |
|---|---|---|---|
| **Alembic trees** | 9 per-module `alembic.ini` + `env.py` + `versions/`; 9 `alembic_version_meho_*` tables; topo sort lived in shell arrays | one `meho_app/alembic/` tree; one `alembic_version` table; ordering via `down_revision` chains | #295 / #299, #300, #301, #302 |
| **Squash probe blocks** | every `0001_squash.py` began with `try: SELECT 1 FROM <table>; return; except: pass` (transaction-aborting on Postgres) | unified `0001_init.py` runs against a clean DB; existing deployments use the rescue script (#300) once | #301 |
| **Module name collision** | `meho_app/modules/connectors/email/` shadowed the Python stdlib `email` module | renamed to `email_connector/`; the database connector type still serializes as `"email"` | #302 |
| **Bootstrap paths** | three (`docker compose up`, `docker-compose.debug.yml`, `dev-env.sh up`/`local`) with mutually inconsistent behavior; `DO NOT USE docker compose DIRECTLY!` warnings on the debug compose | one base + auto-loaded `docker-compose.override.yml` + explicit `docker-compose.test.yml`; one Dockerfile with `prod`/`debug` targets sharing the entrypoint; `meho-dev` Typer CLI replaces 580 lines of bash | #296 / #303, #304, #305, #307, #309, #310, #316, #317, #319 |
| **Loud failure** | `run_migrations_local` had `alembic upgrade head 2>/dev/null \|\| true`, always logged success; `lifespan` had no schema-readiness check, hit `UndefinedTable` deep in stage 2/3; `CREDENTIAL_ENCRYPTION_KEY` had an insecure default | exit codes propagate; `_ensure_schema_ready()` exits with both revisions and three fix commands; `${CREDENTIAL_ENCRYPTION_KEY:?…}` fails compose parse | #297 / #311, #312, #313, #314 |
| **Test fixture** | `metadata.create_all` on 3 of 9 modules; teardown referenced undefined `KnowledgeBase`/`ConnectorsBase`/`AgentBase` (raised `NameError`); broken migrations passed CI | session-scoped `alembic.command.upgrade(cfg, "head")`; per-test `TRUNCATE … RESTART IDENTITY CASCADE`; `tests/.env.test` fallback removed | #298 / #315, #318 |
| **`env.example` drift** | bash `source .env` parsing duplicated Pydantic; new variables landed without documentation | `scripts/check-env-example-sync.py` runs in pre-commit and CI; `.env` is parsed by exactly one parser at runtime | #305 |
| **Operator-facing verify** | no automated success-signal check | `make verify` runs the four greps + env-example sync + advisory `alembic check` + `/health` probe | #467 (this PR) |
| **`make verify` ripgrep guard** | `rg`-based pipeline silently passed when ripgrep was missing | `command -v rg` precondition fails fast with install instructions | #467 (this PR) |

**Open follow-ups surfaced by this work:**

- **#464** — every-PR bootstrap-smoke CI job (`docker compose up` + `/health`). Goal #294 success signal S7; the existing `arm64-first-run.yml` covers ARM64 onboarding only and is path-filtered. Required for merge once it lands.
- **#465** — rescue script `--clean-host-paths` for legacy `meho_app/modules/*/alembic/` directories on in-place upgrades. The runner-side guard (refuse to run if those directories exist) is in this PR; the rescue-side cleanup is the follow-up.
- **#466** — reconcile the residual `alembic check` drift in `0001_init` (FK names, model-side `index=True` declarations, raw HNSW/GIN DDL). Cosmetic; not a behavioral bug, but `alembic revision --autogenerate` produces noise until it's resolved.

## References

- `meho_app/database.py` — engine and session wiring
- `meho_app/core/config.py:36` — `database_url` field definition
- `meho_app/main.py:196` onwards — `lifespan` function
- `meho_app/alembic/env.py` — unified Alembic environment (consolidated under #299)
- `meho_app/alembic/versions/0001_init.py` — consolidated initial schema
- `scripts/run-migrations-monolith.sh` — canonical migration runner
- `scripts/dev-env.sh` — developer bootstrap wrapper
- `scripts/init-db.sql` — postgres first-boot DB creation
- `docker/Dockerfile.meho`, `docker/docker-entrypoint.sh`
- `docker-compose.yml`, `docker-compose.override.yml`, `docker-compose.test.yml`
- `tests/integration/conftest.py` — integration test fixture (`db_session`)
- Upstream Alembic docs: https://alembic.sqlalchemy.org/
- Upstream SQLAlchemy async docs: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html

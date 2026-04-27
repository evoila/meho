<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Bootstrap and Database Migrations

## Overview

This document describes how MEHO.X boots from a clean clone to a running application, and how the database schema is managed across that boot path. It covers the local developer loop, the containerized loop, the test loop, and the production deployment loop.

The area is currently in the middle of a half-finished consolidation from a prior distributed-services layout into a monolith. Many of the scripts, compose files, and Alembic trees in this area are fossils from the distributed era and will be removed as part of the refactor tracked in the issues linked at the bottom of this doc.

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
| 3 | Infrastructure started | postgres, redis, minio, keycloak (and optionally seq, TEI sidecars) | `docker compose up -d` |
| 4 | First-boot SQL | `keycloak` and `keycloak_test` databases created inside postgres | `scripts/init-db.sql` mounted at `/docker-entrypoint-initdb.d/` (runs once per volume) |
| 5 | Health gates | `pg_isready`, `redis-cli ping`, keycloak `/health/ready` all return OK | Compose healthchecks |
| 6 | Migrations applied | All schema changes materialized in postgres | `scripts/run-migrations-monolith.sh` → per-module `alembic upgrade head` |
| 7 | Keycloak configured | Realm, clients, seed tenants created | `scripts/setup-keycloak.sh` |
| 8 | Application lifespan | Topology processor, connector ops sync, skill seeding, stuck-job cleanup, knowledge chunk reconciliation | `meho_app.main:lifespan` |
| 9 | Frontend ready | `meho-frontend` container up, pointing at backend and keycloak | `docker compose up meho-frontend` |

### Invocation paths

All compose-based invocations build the unified `docker/Dockerfile.meho` — `target: prod` from `docker-compose.yml` and `target: debug` from `docker-compose.debug.yml`. Both targets inherit the shared `ENTRYPOINT ["/docker-entrypoint.sh"]` defined in the `base` stage, so migrations run on every compose-driven boot regardless of target:

- **`docker compose up` (plain, against `docker-compose.yml`)** — builds `target: prod`. Entrypoint runs `run-migrations-monolith.sh` before uvicorn starts. Default healthcheck polls `/health`.
- **`docker compose -f docker-compose.debug.yml up`** — builds `target: debug`. Same entrypoint; additionally exposes debugpy on port 5678 and runs uvicorn with `--reload` against the volume-mounted source tree.
- **`./scripts/dev-env.sh up`** — delegates to `docker compose -f docker-compose.debug.yml up -d` and runs `run_migrations()` idempotently via `docker compose exec`. Because the entrypoint already ran migrations, the wrapper's second call is a no-op under normal conditions; it remains as a safety net for the `local` path below.
- **`./scripts/dev-env.sh local`** — starts infrastructure in docker, runs the backend and frontend on the host machine via `uv run`, and calls `run_migrations_local()`, which runs `alembic` against `localhost:5432`. This is the only path that bypasses the docker entrypoint, so the bash-level migration invocation stays load-bearing for host-side development.

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

## The Alembic layout (current state)

MEHO.X currently has **nine** Alembic configurations, one per module:

```
meho_app/modules/
├── knowledge/alembic.ini + alembic/
├── topology/alembic.ini + alembic/
├── connectors/alembic.ini + alembic/
├── memory/alembic.ini + alembic/
├── agents/alembic.ini + alembic/
├── ingestion/alembic.ini + alembic/
├── scheduled_tasks/alembic.ini + alembic/
├── orchestrator_skills/alembic.ini + alembic/
└── audit/alembic.ini + alembic/
```

Each has its own `env.py`, its own `versions/` folder, and its own version-tracking table in the shared `meho` database:

- `alembic_version_meho_knowledge`
- `alembic_version_meho_topology`
- `alembic_version_meho_openapi` (note: the `connectors` module's version table is still named for its pre-rename identity)
- `alembic_version_meho_memory`
- `alembic_version_meho_agents`
- `alembic_version_meho_ingestion`
- `alembic_version_meho_scheduled_tasks`
- `alembic_version_meho_orchestrator_skills`
- `alembic_version_meho_audit`

### Why this is a fossil, not a design

The nine-tree structure is a leftover from a prior distributed-services layout. When the services were merged into one monolith process sharing one database, the per-service Alembic folders were kept in place rather than merged. As a result, the structure today has three properties that make it actively harmful rather than merely costly:

**1. All nine modules share one SQLAlchemy metadata registry.** Every module's `models.py` imports `Base` from the same place (`meho_app.database`), so there is only **one** `Base.metadata` for the whole application. When any module's `env.py` sets `target_metadata = Base.metadata`, it receives the entire cross-module schema, not a slice of it. The per-module isolation is purely a folder-structure illusion.

**2. Autogenerate is therefore unreliable.** Running `alembic revision --autogenerate` against any one module's config compares the entire cross-module schema against the live database. If some modules' model files have not been imported into Python's module cache at the moment autogenerate runs, their tables appear "missing" and autogenerate writes `DROP TABLE` statements for them into what is supposed to be a per-module migration. The `connectors/alembic/env.py` file wraps its model imports in a silent `try/except` that falls back to `target_metadata = None`, which is not defensive paranoia — it's a half-conscious acknowledgment that autogenerate is dangerous here and "safer when broken."

**3. Migration ordering is a manually-maintained topological sort in a shell script.** Because the nine trees share a database with foreign keys between their tables (for example, `knowledge_chunk.connector_id` references the `connector` table), upgrade order matters. The order lives as a hardcoded array in three separate places: `scripts/run-migrations-monolith.sh`, `scripts/dev-env.sh` (inside `run_migrations_local`), and `scripts/stamp-squash.sh`. Adding a module means editing all three. Forgetting one produces silent breakage.

In practice, eight of the nine modules today have only a single migration file (`0001_squash.py`). Only the `connectors` module has real incremental history beyond the squash. The nine-tree structure is carrying four meaningful migrations in nine containers. The ceremony is vastly larger than the payload.

## The squash migrations

Every module has a `0001_squash.py` that was written by hand by reading the old distributed migrations and constructing the final schema as one DDL block. Each squash migration begins with a probe:

```python
def upgrade() -> None:
    conn = op.get_bind()
    try:
        conn.execute(sa.text("SELECT 1 FROM knowledge_chunk LIMIT 1"))
        return  # Existing deployment -- skip DDL
    except Exception:
        pass  # Fresh install -- run DDL below
```

This is a second version-tracking system layered on top of Alembic's real one. It exists to let the same migration file serve both fresh installs and upgrades from pre-squash deployments. The probe has three problems:

- It short-circuits the whole migration if the probe table exists, even if other tables created by the same migration do not. A partially-broken schema can make the squash file do nothing.
- It catches `Exception`, which swallows connection errors, permission errors, and schema-search-path issues as well as "table doesn't exist."
- It duplicates functionality that `alembic_version_meho_*` tables already provide. The correct approach is to trust the version table and keep the squash migration idempotent only in the `None → squash_001` direction.

The companion one-time rescue script `scripts/stamp-squash.sh` exists to stamp existing deployments at `squash_001` without re-running DDL. Between the probe and the stamp script, there are two mechanisms trying to solve the same problem and they do not know about each other.

## Migration runner paths

### `scripts/run-migrations-monolith.sh`

The canonical runner. Loops through a hardcoded array of 9 module directories in dependency order and runs `python3 -m alembic -c <module>/alembic.ini upgrade head` for each. Called from:

- `docker/docker-entrypoint.sh` (the production Dockerfile's entrypoint)
- `scripts/dev-env.sh`'s `run_migrations()` function (via `docker compose exec meho`)

A comment in this script documents a load-bearing Python pitfall: the `connectors/email/` folder name collides with Python's stdlib `email` module. `cd`-ing into `connectors/` causes `import email` to resolve to the local folder, crashing with a circular import. The workaround is to always invoke Alembic with `-c <path>/alembic.ini` from the repo root, never by changing directories. This workaround must be respected by every developer and every future migration tool invocation until the `email/` folder is renamed.

### `scripts/dev-env.sh` — two functions, two behaviors

`run_migrations()` (used by `dev-env.sh up`) shells into the running `meho` container and runs `run-migrations-monolith.sh`. Failures propagate correctly.

`run_migrations_local()` (used by `dev-env.sh local`) is a **parallel reimplementation**: it exports a hardcoded `DATABASE_URL=postgresql+asyncpg://meho:password@localhost:5432/meho`, loops through a separate hardcoded module list, and invokes system `alembic` directly. Crucially, it suppresses all stderr and ignores all exit codes:

```bash
alembic -c "${alembic_ini}" upgrade head 2>/dev/null || true
```

The function therefore always reports "✅ Migrations complete" regardless of actual outcome. This single line is the largest source of "my colleague says migrations ran fine but the app still crashes" reports.

### `scripts/check-migrations.sh`

A fail-fast check that queries `information_schema.tables` for the existence of `knowledge_chunk`. Currently not wired into any other script — it exists in the repo but is not called by `docker-entrypoint.sh`, `dev-env.sh`, or any CI workflow. It is a runnable but orphaned health probe.

### Legacy scripts (to be removed)

- `scripts/reset-db.sh` and `scripts/migrate-down.sh` still reference pre-monolith service names (`meho_agent`, `meho_openapi`, `meho_knowledge`) as directories that no longer exist. Both are 100% dead.
- `scripts/migrate_knowledge_alembic_version.py` is a one-time upgrade script from a prior rename of the `alembic_version` table to `alembic_version_meho_knowledge`. Any deployment that would still need it has other, larger problems.

## Docker image layout

A single Dockerfile at `docker/Dockerfile.meho` builds every backend image via multi-stage targets:

- `base` — shared stage. Installs apt deps, drops in the `uv` static binary, runs `uv sync --frozen --no-install-project` for optimal layer caching, copies the project source, runs `uv sync --frozen` to finalize the project install, and declares the shared `ENTRYPOINT ["/docker-entrypoint.sh"]`. Optional heavy dependency groups are gated by `ARG INCLUDE_DOCLING=false` and `ARG CUDA_ENABLED=false`, passed at build time via `--build-arg`. The runtime env var `MEHO_FEATURE_USE_DOCLING` is wired to `INCLUDE_DOCLING` so the app's flag-gated Docling import behaves correctly without additional configuration.
- `prod` — production image. Inherits `base`. `CMD` runs uvicorn directly. Defines a `HEALTHCHECK` that polls `/health`.
- `debug` — development image. Inherits `base`. Adds `uv sync --frozen --group dev` for ruff/mypy/pytest-watch/ipython/debugpy. `CMD` wraps uvicorn in debugpy on port 5678 with `--reload` against the volume-mounted source tree. No `--no-editable`, so edits land immediately.

CPU vs GPU is a separate axis, selected by `--build-arg TARGETBASE=base-cpu` (default, `python:3.13-slim`) or `base-gpu` (`nvidia/cuda:12.4.1-runtime-ubuntu22.04`).

The frontend has its own Dockerfile (`docker/Dockerfile.meho-frontend`) which has no database touchpoint and is not discussed here.

## Compose file layout

Three compose files exist, each a standalone definition rather than a layered override:

- `docker-compose.yml` — the full production-ish stack. Builds `docker/Dockerfile.meho` with `target: prod`, uses `redis-stack-server` (which provides the `redisearch` and `rejson` modules the application requires), mounts `init-db.sql`, includes Keycloak, Seq, and optional TEI sidecars. `CREDENTIAL_ENCRYPTION_KEY` has a weak placeholder default (pre-existing; see the follow-up work tracking the move to required-variable interpolation across both compose files).
- `docker-compose.debug.yml` — the hot-reload stack. Builds `docker/Dockerfile.meho` with `target: debug`, exposes debugpy on 5678, currently mirrors most of `docker-compose.yml`'s service definitions. Uses Compose's required-variable syntax for `CREDENTIAL_ENCRYPTION_KEY` (`${VAR:?...}`), so `docker compose up` fails fast with a generate-a-key hint if the env var is missing. Collapsing this file into a `docker-compose.override.yml`-style layer on top of the main compose is tracked as follow-up refactor work.
- `docker-compose.test.yml` — the CI stack. Uses `tmpfs` for postgres and minio (ephemeral storage, no persistence between runs). Boots a separate `keycloak_test` database.

The three files drift independently. A developer switching between compose files gets a different app each time, with different services, different images, different required environment variables, and different failure modes. Docker Compose natively supports base + override composition (`docker compose -f base.yml -f override.yml`) but this repo does not use it.

## Application startup (`lifespan`)

When the backend container starts, `uvicorn meho_app.main:app` runs the `lifespan` async context manager (defined in `meho_app/main.py`). The lifespan performs three categories of work, in order, all inside the same ~250-line function:

1. **Background worker startup** — if `flags.topology` and `topology_auto_discovery_enabled` are set, initialize a Redis-backed discovery queue and start a batch processor.
2. **Knowledge base synchronization** — if `flags.knowledge` is set, cleanup any ingestion jobs left in a stuck state by a previous crash, sync typed connector operations into the knowledge base, and reconcile knowledge chunks.
3. **Seed data** — ensure baseline orchestrator skills exist for every tenant in the database.

All three stages assume the schema is up to date. There is no explicit check against `alembic_version_meho_*` before the lifespan begins executing. When migrations have not run, the first stage to touch the database raises an `UndefinedTable` error, the container exits, and Docker restarts it into an infinite loop. The root cause ("migrations were not applied") is never stated in the logs — the developer sees only the symptom ("relation 'topology_node' does not exist").

Each of the three stages wraps itself in a bare `try/except` that logs a warning and continues. This is intended as graceful degradation — a non-fatal failure in one stage should not prevent the others from running — but it has the side effect that a schema-readiness failure in stage 1 silently disables stages 2 and 3 as well, and each produces its own confusing error message.

## Integration test schema setup

The `db_session` fixture in `tests/integration/conftest.py` builds the test schema by calling `Base.metadata.create_all` directly on three modules' Base objects:

```python
if knowledge_base_cls:
    await conn.run_sync(knowledge_base_cls.metadata.create_all)
if connectors_base_cls:
    await conn.run_sync(connectors_base_cls.metadata.create_all)
if agent_base_cls:
    await conn.run_sync(agent_base_cls.metadata.create_all)
```

This bypasses Alembic entirely. Three consequences follow:

1. **Tests build the schema from models, production builds it from migrations.** These two definitions can drift indefinitely. A migration file can be broken in a way that the test suite will never catch, because the test suite never reads the migration file.
2. **Only three of the nine modules are loaded.** Topology, memory, ingestion, scheduled_tasks, orchestrator_skills, and audit are not initialized in the test fixture. Integration tests touching those modules will fail with "table does not exist."
3. **The teardown path references symbols that do not exist.** The fixture attempts to call `drop_all` on `KnowledgeBase`, `ConnectorsBase`, and `AgentBase` — names that are never imported (the actual imports use lowercase suffixes like `knowledge_base_cls`). The teardown raises `NameError`, tables are not dropped between tests, and test state leaks across runs in ways that make failures order-dependent.

The combined effect is that the integration test suite cannot catch migration bugs and cannot reliably isolate tests from each other.

## The "new migration" blast radius today

To make the failure mode concrete, here is what happens when a developer adds a new column to a model today:

1. The developer edits `meho_app/modules/connectors/models.py` and adds a column.
2. They run `alembic revision --autogenerate -c meho_app/modules/connectors/alembic.ini -m "add column"`. Alembic compares the **cross-module** `Base.metadata` against the local database. Depending on which modules' models have been imported at that moment, the generated migration file may contain spurious `DROP TABLE` statements for unrelated modules. The developer eyeballs the file and cleans it up — or does not.
3. They run `./scripts/dev-env.sh local`. The local migration path reports success regardless of actual outcome.
4. Their own database is now at the new revision. Their tests pass because the tests use `metadata.create_all`, not Alembic.
5. They commit and push.
6. A colleague pulls the branch. Their `postgres_data` volume still has the old schema. They run `./scripts/dev-env.sh local`. The silent-failure path reports success; one of the migration files errors out; the colleague's database is partially migrated.
7. The colleague's `lifespan` hits an `UndefinedColumn` error. The container restarts in a loop. The root cause is not surfaced.
8. The colleague debugs in a mix of `docker compose logs`, reading scripts, checking `alembic_version_meho_*` tables by hand, and eventually asks the author for help.

Every step of this loop is an audit finding linked at the bottom of this document.

## Key files

| Concern | File | Role |
|---|---|---|
| Engine + session wiring | `meho_app/database.py` | Lazy global async engine and session maker; shared `Base` |
| App configuration | `meho_app/core/config.py` | Pydantic Settings; `DATABASE_URL` required, no default |
| App startup | `meho_app/main.py` `lifespan` | Topology processor, connector sync, skill seeding — all assume schema ready |
| Canonical migration runner | `scripts/run-migrations-monolith.sh` | Bash loop over hardcoded module list |
| Container entrypoint | `docker/docker-entrypoint.sh` | Runs migrations then `exec`s the image CMD |
| Backend image (unified) | `docker/Dockerfile.meho` | Multi-stage; `prod` and `debug` targets share `ENTRYPOINT` |
| Prod compose | `docker-compose.yml` | Full stack; redis-stack; builds `target: prod` |
| Debug compose | `docker-compose.debug.yml` | Builds `target: debug`; exposes debugpy on 5678 |
| Test compose | `docker-compose.test.yml` | tmpfs postgres/minio |
| Bootstrap wrapper | `scripts/dev-env.sh` | Two migration functions with different behaviors |
| Postgres init | `scripts/init-db.sql` | Creates `keycloak` and `keycloak_test` databases (first boot only) |
| Integration fixtures | `tests/integration/conftest.py` | Bypasses Alembic; only loads 3 of 9 modules |
| Legacy (to be removed) | `scripts/reset-db.sh`, `scripts/migrate-down.sh`, `scripts/migrate_knowledge_alembic_version.py`, `scripts/check-migrations.sh`, `scripts/stamp-squash.sh` | Pre-monolith fossils or one-time rescue scripts no longer needed |

## The refactor direction

The audit findings cluster into four themes. The refactor should advance all four in parallel; fixing one without the others leaves the "works on my machine" loop open.

**1. Consolidate the Alembic trees.** Merge the nine per-module `alembic.ini` + `env.py` + `versions/` folders into a single tree at `meho_app/alembic/`. One `alembic_version` table. One linear history. Autogenerate becomes reliable because `target_metadata` unambiguously covers the whole application. The hardcoded ordering arrays in the migration scripts disappear because there is only one history to apply.

**2. Unify the bootstrap paths.** One entrypoint script, used by both the prod and debug Dockerfiles. One migration invocation, used by both `dev-env.sh up` and `dev-env.sh local`. One base compose file with overrides for debug and test. The goal is that `docker compose up` works from a clean clone without any wrapper script — the wrapper should exist only for convenience, not as a prerequisite for correctness.

**3. Fail loud, not silent.** Remove `2>/dev/null` and `|| true` from every migration invocation. Add a schema-readiness check at the top of `lifespan()` that compares the current `alembic_version` to the expected head and exits with an actionable message if they differ. Remove the probe blocks from the squash migrations and trust Alembic's version tracking. Remove the insecure encryption-key fallback from the prod compose file.

**4. Run migrations in tests.** Replace the `metadata.create_all`-based `db_session` fixture with one that applies real Alembic migrations against an empty test database at session start, then uses per-test `TRUNCATE` for isolation. This closes the gap where a broken migration can pass CI. Add a bootstrap smoke test to CI that runs the full `docker compose up` path and hits `/health`.

## Known issues (audit findings)

The findings below are the result of the audit that produced this document. Each has a corresponding GitHub issue in project 16. Issues are listed here by theme, cross-referenced to the "blocker / high / medium / nit" severity assigned in the audit.

**Consolidate**
- Merge nine Alembic trees into one (**blocker**) — `[issue link pending Phase 6]`
- Remove probe blocks from squash migrations, trust version tables (**high**) — `[issue link pending]`
- Delete hardcoded module-order arrays from shell scripts (resolved by consolidation, **high**) — `[issue link pending]`
- Rename `email/` folder to avoid stdlib collision (**high**) — `[issue link pending]`
- Delete the nine duplicate `alembic.ini` boilerplate files (resolved by consolidation, **medium**) — `[issue link pending]`
- Rename `alembic_version_meho_openapi` table to match module name (**nit**) — `[issue link pending]`

**Unify**
- Add `ENTRYPOINT` to debug Dockerfile, or unify to one Dockerfile with build targets (**blocker**) — `[issue link pending]`
- Convert three compose files to base + overrides (**high**) — `[issue link pending]`
- Single `.env` parser: stop `source`-ing in bash, stop ad-hoc parsing (**medium**) — `[issue link pending]`
- Delete legacy scripts (`reset-db.sh`, `migrate-down.sh`, `migrate_knowledge_alembic_version.py`, `check-migrations.sh`) (**medium**) — `[issue link pending]`
- Reconcile README and debug-compose warning — one documented command that actually works (**medium docs**) — `[issue link pending]`
- Write `docs/contributing/migrations.md` — how to author a migration (**medium docs**) — `[issue link pending]`
- Document the "new module onboarding" checklist (resolved by consolidation) — `[issue link pending]`

**Fail loud**
- Remove `2>/dev/null || true` from `run_migrations_local()` (**blocker**) — `[issue link pending]`
- Fix or delete the broken `db_session` teardown that references undefined names (**blocker**, resolved by the migrate-in-tests work) — `[issue link pending]`
- Remove insecure `CREDENTIAL_ENCRYPTION_KEY` default from prod compose (**high**) — `[issue link pending]`
- Add schema-readiness check at the top of `lifespan()` (**high**) — `[issue link pending]`
- Split `lifespan()` into named phases (schema-ready, workers, seed) (**medium**) — `[issue link pending]`

**Migrate in tests**
- Replace `metadata.create_all` with Alembic-driven fixture (**blocker**) — `[issue link pending]`
- Add bootstrap smoke test to CI (**medium**) — `[issue link pending]`

## References

- `meho_app/database.py` — engine and session wiring
- `meho_app/core/config.py:36` — `database_url` field definition
- `meho_app/main.py:196` onwards — `lifespan` function
- `meho_app/modules/*/alembic/env.py` — per-module Alembic environment files
- `meho_app/modules/*/alembic/versions/0001_squash.py` — consolidated initial migrations
- `scripts/run-migrations-monolith.sh` — canonical migration runner
- `scripts/dev-env.sh` — developer bootstrap wrapper
- `scripts/init-db.sql` — postgres first-boot DB creation
- `docker/Dockerfile.meho`, `docker/docker-entrypoint.sh`
- `docker-compose.yml`, `docker-compose.debug.yml`, `docker-compose.test.yml`
- `tests/integration/conftest.py` — integration test fixture (`db_session`)
- Upstream Alembic docs: https://alembic.sqlalchemy.org/
- Upstream SQLAlchemy async docs: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html

# `scripts/`

Operational and developer-facing scripts for MEHO. This directory is curated:
new scripts must be classified into one of the buckets below and added to the
inventory before merging. Anything that does not fit a bucket is a code smell.

The classification was created in #316 / PR-L. Five pre-monolith fossils
(`reset-db.sh`, `migrate-down.sh`, `migrate_knowledge_alembic_version.py`,
`check-migrations.sh`, `stamp-squash.sh`) were removed in #306 / PR-K. Three
additional bucket-D fossils (`teardown.sh`, plus the now-rewritten `lint.sh`
and `typecheck.sh` whose pre-monolith bodies referenced six dead packages)
were either deleted or rewritten in this audit. The Python rescue tools and
one-shot scripts that were retained live alongside the operational tooling
because they are still safe to run.

## Buckets

- **A -- operational.** Required for normal workflows. Invoked by users,
  Docker entrypoints, CI, pre-commit, or the mirror pipeline. Must always
  work.
- **B -- wrappers.** Thin convenience shells around `pytest`, `docker
  compose`, `uv run`, etc. Slated for migration to `meho-dev` Typer
  subcommands in #310 / PR-M. Until then, kept here so muscle-memory and the
  `Makefile` keep working.
- **C -- operator / maintenance tools.** Runnable when an operator needs
  them; not part of routine workflows. Kept as documented Python scripts
  rather than CLI subcommands because they have low call frequency and high
  blast radius.
- **D -- delete on sight.** Anything that references pre-monolith package
  names (`meho_core`, `meho_knowledge`, `meho_openapi`, `meho_agent`,
  `meho_ingestion`, `meho_api`), pre-monolith Docker container names
  (`meho-qdrant`, `meho-rabbitmq`, `meho-{api,knowledge,openapi,agent,
  ingestion}`), or one-time migration steps that have already executed
  in production. New bucket-D scripts must not be added; old ones get
  removed when found.

## Inventory (current)

### Bucket A -- operational

| Script | Role | Invoked by |
| --- | --- | --- |
| `assemble-public-tree.sh` | Builds the public-mirror tree from the private repo | `mirror-to-public.yml` workflow |
| `check-env-example-sync.py` | Verifies `env.example` matches every Pydantic `BaseSettings` class (#305) | `pre-commit`, `ci.yml` |
| `create-k8s-service-account.sh` | Helper for users wiring the Kubernetes connector | Docs / operator on demand |
| `dev-env.sh` | Bootstrap orchestrator (`up`, `down`, `local`, ...) | Developers, `make dev-up`, `make dev-down` |
| `dev-setup.sh` | Installs pre-commit hooks; first-clone setup | Developers (one-time per clone) |
| `generate-encryption-key.sh` | Generates a Fernet key for `CREDENTIAL_ENCRYPTION_KEY` (#312) | Operators / `env.example` instructions |
| `generate-license-keypair.py` | Generates the Ed25519 keypair for license signing | Release engineering (one-shot but kept) |
| `generate-openapi.py` | Dumps the FastAPI OpenAPI spec for the docs site | MkDocs build, CI |
| `hooks/block-public-remote-push.sh` | Pre-push guard that refuses pushes to the public remote | `.git/hooks/pre-push` (via `dev-setup.sh`) |
| `init-db.sql` | Postgres image initdb hook (creates `keycloak`/`keycloak_test`) | `docker-compose.yml` |
| `migrate_to_unified_alembic.py` | One-shot rescue that stamps existing 9-tree deployments at the unified head (#300) | Operators of pre-#299 deployments |
| `preflight.sh` | Pre-bootstrap environment checks (#261) | Developers, CI smoke |
| `run-migrations-monolith.sh` | Runs `alembic -c meho_app/alembic.ini upgrade head` | `docker-entrypoint.sh`, `dev-env.sh` |
| `setup-keycloak.sh` | Imports the realm + creates a service account for first-run users | Developers / operators on first run |
| `validate-install.sh` | Smoke test that exercises the bootstrap end-to-end (#261) | Developers, CI smoke |
| `validate-services.sh` | Health-checks the running services (#261) | Developers, CI smoke |

### Bucket A -- code-quality wrappers (not yet ported, but used by Makefile and CI)

| Script | Role | Invoked by |
| --- | --- | --- |
| `add_spdx_headers.py` | Stamps SPDX/copyright headers on new source files | Developers; pre-commit catches drift today |
| `lint.sh` | `ruff check` + `ruff format --check` + `mypy meho_app/` | `make lint`, developers |
| `typecheck.sh` | `mypy meho_app/` (with `--quiet` summary mode) | `dev-env.sh tests`, `run-critical-tests.sh` |

### Bucket B -- thin wrappers (port to `meho-dev` Typer in PR-M / #310)

| Script | Wraps | Notes |
| --- | --- | --- |
| `re-embed.sh` | `docker exec ... python scripts/re_embed_voyage.py` | Will become `meho-dev re-embed` |
| `run-critical-tests.sh` | smoke + contracts pytest + `typecheck.sh` | Will become `meho-dev test critical` |
| `run-e2e-tests.sh` | docker compose + e2e pytest | Will become `meho-dev test e2e` |
| `run-integration-tests.sh` | integration pytest | Will become `meho-dev test integration` |
| `run-tests.sh` | pytest | Will become `meho-dev test` |
| `run-unit-tests.sh` | unit pytest | Will become `meho-dev test unit` |
| `seed-connectors.sh` | API calls that POST connector configs | Will become `meho-dev seed connectors` |
| `test-env-up.sh` | `docker compose -f base.yml -f test.yml up -d` | Will become `meho-dev test up` |
| `test-env-down.sh` | `docker compose -f base.yml -f test.yml down -v` | Will become `meho-dev test down` |
| `watch-tests.sh` | `pytest-watch` | Will become `meho-dev test watch` |

### Bucket C -- operator / maintenance Python tools

| Script | When to use |
| --- | --- |
| `benchmark_orchestrator.py` | Performance investigation of the orchestrator agent. Kept as a developer tool. |
| `cleanup_orphaned_topology.py` | One-time-style cleanup for topology entities whose `connector_id` references deleted connectors. Re-runnable. |
| `re_embed_voyage.py` | Regenerates knowledge-chunk embeddings after model upgrades. Wrapped by `re-embed.sh`. |
| `resync-connector-operations.py` | Backfills response-schema fields onto existing `connector_operation` rows after a model change. |

### `archive/`

Historical scripts kept for reference only. Not on any execution path. See
`scripts/archive/README.md` for the per-script index.

## Adding a new script

1. Pick the bucket. If you cannot, the script is probably duplicating
   existing functionality -- add to an existing one instead.
2. Make it executable (`chmod +x`) for shell scripts. Add the SPDX header
   for `.sh` and `.py` (the pre-commit `spdx-headers` hook will refuse the
   commit otherwise).
3. Add a row to the inventory above.
4. Avoid hard-coding port numbers, container names, or paths -- use
   `${PROJECT_ROOT}` (computed from `BASH_SOURCE[0]`) and read the rest
   from `${VAR:-default}` so the script keeps working when the environment
   shifts.

## What this directory does not have

- No `__pycache__` (root `.gitignore` covers it).
- No `*.bak`, `*.orig` rescue files (`.gitignore` covers them).
- No `.env`, `.env.test`, or any other environment file (test config flows
  through `tests/support/test_config.py`, not files in `scripts/`).

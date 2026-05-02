# Authoring database migrations

> One page. If you can't write the migration after reading this, the page is wrong -- file a doc bug.

MEHO has **one** Alembic tree at `meho_app/alembic/` and **one** `alembic_version` row at runtime. The pre-Goal-#294 layout (nine `meho_app/modules/*/alembic/` directories, nine `alembic_version_meho_*` tables) is gone. Read [docs/codebase/bootstrap-and-migrations.md](../codebase/bootstrap-and-migrations.md) for the full archaeology if you need it.

## Authoring workflow

The autogenerate workflow is the same whether you're adding a column, a table, or a whole module's worth of tables. Edit the SQLAlchemy model first, then ask Alembic to diff the model graph against the database.

```bash
# 1. Edit your SQLAlchemy model under meho_app/modules/<module>/models.py.

# 2. Make sure your dev database is at head -- autogenerate compares
#    DB schema against models, so a stale DB will produce nonsense diffs.
docker compose exec meho uv run alembic -c meho_app/alembic.ini upgrade head

# 3. Generate the new revision. The message goes after -m and shows up in the
#    docstring of the generated file.
docker compose exec meho uv run alembic -c meho_app/alembic.ini \
    revision --autogenerate -m "add idempotency_key to ingestion_jobs"

# 4. Open the generated file in meho_app/alembic/versions/. Read it, *edit it*.
#    Autogenerate is a starting point, not a finished migration -- see "What
#    autogenerate gets wrong" below.

# 5. Apply the migration locally and verify your endpoint / model works.
docker compose exec meho uv run alembic -c meho_app/alembic.ini upgrade head

# 6. Verify the diff is empty -- the model graph should now match the DB.
docker compose exec meho uv run alembic -c meho_app/alembic.ini check
```

`alembic check` is the most useful gate. If it reports differences after step 5, your model and your DDL disagree.

## File naming and revision IDs

Migrations live in `meho_app/alembic/versions/` and follow a strict naming pattern: `NNNN_short_slug.py`, where `NNNN` is a zero-padded sequential integer matching the `revision` string inside the file. The post-#294 history starts at `0001_init.py`; pick the next free number.

The header inside the file is filled in by `meho_app/alembic/script.py.mako`. Always set `revision`, `down_revision`, and a docstring that names the change in user terms (not implementation terms -- "add idempotency_key" is good, "fix duplicate inserts" is not).

## Four things you do not do

These are not stylistic preferences. CI, the schema-readiness gate (#313), or production data integrity will break if you do any of them.

1. **Do not modify a migration that has been merged to `main`.** The Alembic graph is append-only across the team. If a merged migration is wrong, write a new migration that fixes it. Editing a committed file leaves every other developer's database stamped at a revision whose definition silently changed -- the kind of incident that takes a Friday afternoon to debug.
2. **Do not add a probe-and-bail "if table exists" block at the top of `upgrade()`.** The pre-#294 squash migrations did this to paper over the mismatched per-module trees. The unified tree starts from a clean `0001_init`; conditional probes only hide schema drift now. If your migration needs guarded DDL (e.g., extension creation), use the explicit `CREATE EXTENSION IF NOT EXISTS` form -- not a Python `try/except`.
3. **Do not silence migration failures with `2>/dev/null` or `|| true`.** Issue #311 deleted the last instance of this in `scripts/dev-env.sh`. The schema-readiness gate (`_ensure_schema_ready` in `meho_app/main.py`) refuses to serve traffic against a stale schema, but only if the migration runner actually fails loud. A swallowed `alembic` exit code is the worst possible failure mode -- the app starts, takes traffic, and 500s on the first query that touches the new table.
4. **Do not add a new `alembic.ini` anywhere in the repo.** There is exactly one, at `meho_app/alembic.ini`. New modules import their models from `meho_app/alembic/env.py` so `Base.metadata` sees them. The pre-#294 per-module tree is the bug, not the model.

## What autogenerate gets wrong

`alembic revision --autogenerate` is a SQLAlchemy-side schema differ. It reads the model graph and the live database, and emits Python that should make them match. It is reliable for column adds, drops, type changes, and table creates. It is **unreliable** for:

- **Index renames.** Autogenerate sees a drop + create instead of a rename. If you renamed an index for clarity, replace the generated drop/create with `op.execute("ALTER INDEX ... RENAME TO ...")`.
- **Server-side defaults that contain SQL functions** (e.g., `server_default=sa.func.now()`). Autogenerate often "corrects" these by emitting a default change every time it runs. Set the default explicitly in the migration and use `existing_server_default=` on subsequent alters.
- **CHECK constraints.** Autogenerate ignores most of them. Write `op.create_check_constraint(...)` by hand.
- **Data migrations.** Autogenerate never produces them. If a column's semantics change (rename + reformat, NULL → default, splitting a JSON blob into typed columns), write the data migration explicitly using `op.execute(...)` or a SQLAlchemy `Connection`.
- **Enum changes on PostgreSQL.** PostgreSQL doesn't allow you to remove enum values, and adding one outside a transaction requires `op.get_bind().execute(text("ALTER TYPE ... ADD VALUE ..."))` with care. Autogenerate produces a `sa.Enum(...)` literal that drops and recreates the type -- that's almost never what you want.

When autogenerate produces something that looks wrong, edit the file. The generated code is yours to own from the moment you commit it.

## Downgrades

Every migration in `meho_app/alembic/versions/` ships a `downgrade()` body. We don't run downgrades in production -- forward-only migrations are the contract -- but the function exists so a developer can roll back during local debugging without dropping their database. If your migration legitimately can't be reversed (e.g., it dropped a column with no copy of the data), explain why in a comment in `downgrade()` and `raise NotImplementedError(...)` rather than silently no-op.

## Bootstrap, lifespan, and the rescue script

Three pieces of code consume migrations at startup -- understand them before merging anything risky:

| Piece | Path | What it does |
|---|---|---|
| Container migration runner | `scripts/run-migrations-monolith.sh` | One-line wrapper around `alembic upgrade head`. Non-zero exit aborts container startup. |
| Lifespan schema gate | `meho_app/main.py` (`_ensure_schema_ready`) | At app boot, compares the DB's `alembic_version` to the head revision the wheel knows about. Mismatch raises `SystemExit` with the exact `alembic upgrade head` command to run. Tested in `tests/unit/test_lifespan_schema_gate.py`. |
| Existing-deployment rescue | `scripts/migrate_to_unified_alembic.py` | One-time script for instances upgrading from the pre-#294 nine-tree layout. Stamps the new `alembic_version` and drops the legacy `alembic_version_meho_*` tables. Documented in [docs/deployment.md](../deployment.md#upgrading-existing-deployments-rescue-script). |

If your migration is irreversible or has a long-running data backfill, mention the schema gate's behavior in your PR -- developers pulling your branch will see the loud `SystemExit` if their dev DB hasn't been migrated yet, and that's expected.

## Integration tests

Integration tests run the entire unified Alembic tree once per session via the `_migrate_test_database` fixture in `tests/integration/conftest.py`, then `TRUNCATE ... RESTART IDENTITY CASCADE` between tests. You don't need to do anything special -- new migrations are picked up automatically. If a migration fails at fixture setup, pytest will name Alembic in the error, not the model.

If your migration changes the schema in a way that breaks existing integration tests (e.g., a NOT NULL column with no default), update the test fixtures in the same PR. The pre-#315 layout silently skipped six modules' tables; the new fixture surface area is wider, so a migration that "passes locally" without integration tests is more likely to break CI than it used to be.

## Related reading

- Reality before #294: [docs/codebase/bootstrap-and-migrations.md](../codebase/bootstrap-and-migrations.md)
- Operator-facing runbook (rescue script, troubleshooting): [docs/deployment.md](../deployment.md)
- Onboarding walkthrough: [docs/getting-started.md](../getting-started.md)
- AGENTS.md: the absolute rules at the repo root, including "never modify a committed migration"

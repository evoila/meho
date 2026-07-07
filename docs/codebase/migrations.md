# Alembic migrations

## Overview

MEHO uses [Alembic](https://alembic.sqlalchemy.org/) for database schema
management. Migrations live in `backend/alembic/versions/` as numbered Python
files (`0001_*.py`, `0002_*.py`, …). The chain runs against both SQLite
(development and unit tests) and PostgreSQL (staging and production).

The Alembic environment (`backend/alembic/env.py`) uses an async cookbook so
migrations run via the same `asyncio`-aware engine the application uses. Each
migration file's `upgrade()` and `downgrade()` functions run synchronously
inside an `asyncio.run()` call made by Alembic — do not `await` in migration
bodies; use synchronous SQLAlchemy Core (`op.get_bind()` / `bind.execute()`).

## File naming

```
<sequence>_<slug>.py
```

Sequence is zero-padded to four digits (`0001`, `0018`, …). The slug is
lower-case with underscores. Every new migration increments the sequence by one.

## UUID binding convention

**The most important rule for data migrations.**

SQLAlchemy's `Uuid(as_uuid=True)` column type (the project-wide default for
every UUID primary key and UUID foreign key) stores values differently per
dialect:

| Dialect    | Storage format         | Example                                |
|------------|------------------------|----------------------------------------|
| PostgreSQL | Native `uuid` type     | `fc8c7b96-89f9-4d0d-b164-7f3e8f29bd8d` |
| SQLite     | `CHAR(32)` hex string  | `fc8c7b9689f94d0db1647f3e8f29bd8d`      |

SQLite's bind processor calls `value.hex` — the 32-character hex form without
dashes. A data migration that writes `str(uuid_value)` (the 36-character
canonical dashed form) stores a string the ORM's FK lookup never matches,
because the ORM issues `WHERE id = <value.hex>` against SQLite rows.

**Always use `value.hex` on SQLite, not `str(uuid_value)`.**

### Canonical pattern

Every data migration that writes a UUID bind parameter must detect the dialect
and pass the right form:

```python
def _uuid_param(value: uuid.UUID, *, is_postgres: bool) -> object:
    """Return the correct bind type for a UUID on each dialect.

    On PostgreSQL the asyncpg driver accepts ``uuid.UUID`` objects
    directly.  On SQLite the stdlib sqlite3 driver does not register a
    UUID adapter, so pass the 32-char hex string (``value.hex``) to
    match the ``Uuid(as_uuid=True)`` bind processor's output.
    """
    return value if is_postgres else value.hex
```

Detect the dialect inside `upgrade()`:

```python
def upgrade() -> None:
    bind = op.get_bind()
    is_postgres: bool = bind.dialect.name == "postgresql"
    tenant_id = uuid.uuid4()
    bind.execute(
        sa.text("INSERT INTO tenant (id, slug) VALUES (:id, :slug)"),
        {"id": _uuid_param(tenant_id, is_postgres=is_postgres), "slug": "rdc-internal"},
    )
```

### Why `value.hex` not `str(value)`

`str(uuid.UUID(...))` produces the 36-char dashed form (`fc8c7b96-89f9-...`).
SQLAlchemy's bind processor for `Uuid(as_uuid=True)` on SQLite calls
`value.hex`, yielding the 32-char compact form (`fc8c7b9689f9...`). When a
migration stores the dashed form in a `CHAR(32)` column, ORM-issued FK joins
compare `fc8c7b96-89f9-4d0d-b164-7f3e8f29bd8d` against
`fc8c7b9689f94d0db1647f3e8f29bd8d` — bytewise mismatch, silently no rows
found.

This caused 88 cascading test failures in PR #1045 (G7.1-T5). The fix was a
one-line change in `0018_seed_rdc_internal_conventions.py`.

### Reading UUIDs back from the DB

SQLite via aiosqlite returns UUID-column values as plain strings (the `CHAR(32)`
storage value). PostgreSQL returns `uuid.UUID` objects. Normalise before
passing back as bind params:

```python
def _coerce_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))  # str() here is safe: converting a str to UUID object
```

The `uuid.UUID(str(value))` call accepts both the 32-char hex and the 36-char
dashed forms and returns a proper `uuid.UUID` object; the returned object is
then passed to `_uuid_param` to get the dialect-correct bind value.

### Drift guard

`backend/tests/migrations/test_alembic_uuid_param_consistency.py` contains an AST-based
scanner that runs on every CI push and fails when any migration uses
`str(<uuid-ish>)` as a dict value in a bind parameter context. It also
contains a parametrised audit that checks each migration file individually
for granular CI failure attribution.

## Migration types

### Schema migrations (DDL-only)

The common case: `op.create_table()`, `op.add_column()`, `op.create_index()`,
`op.create_foreign_key()`. No data operations, no UUID bind params.

### Data migrations (seed / backfill)

Migrations that INSERT, UPDATE, or DELETE rows. Rules:

1. **Self-contained.** Do not import ORM models — models can change; the
   migration must be a frozen snapshot of the schema at the revision's
   point in time. Use `sa.table()` / `sa.column()` to reflect tables.
2. **Parameterised SQL.** Use `sa.text("... :param ...")` with named bind
   parameters — never f-string interpolation.
3. **UUID bind convention.** Use `_uuid_param(value, is_postgres=is_postgres)`
   (see above).
4. **Timestamps.** `now()` is PG-only. For SQLite, pass a Python
   `datetime.now(UTC).isoformat()` string as a bind parameter.
5. **Idempotent.** `ON CONFLICT DO NOTHING` / `ON CONFLICT DO UPDATE` so a
   re-run on an already-migrated DB is a no-op.
6. **Reversible — or a documented no-op.** Seed migrations undo their data in
   `downgrade()` (delete seeded rows by a stable discriminant such as
   `actor_sub` or slug) without touching rows the operator authored
   independently. Backfill-rewrite migrations (`0011`, `0038`) instead ship a
   **documented no-op** `downgrade()` when the pre-upgrade value carries no
   operator-recoverable state (restoring it would need a copy column, and the
   restored value serves no one). The no-op must say *why* in its docstring —
   explicit refusal beats silent partial recovery.
7. **Guard natural-key rewrites against collisions.** An UPDATE that rewrites
   part of a unique key (e.g. `0038` rewriting `product`) must carry a
   correlated `NOT EXISTS` twin check on the target key, or a row inserted
   under the new spelling after the fix-forward release will make the
   migration Job die with `IntegrityError` mid-deploy. Skip-and-leave beats
   delete: the skipped row stays in its pre-migration state, and destructive
   cleanup remains operator-driven.

## Additive-only constraint

Migrations in `upgrade()` must be additive: `create_table`, `add_column`,
`create_index`, `create_foreign_key`, or data backfills. Destructive DDL
(`drop_column`, `drop_table`, `rename_table`, `alter_column nullable=False`,
raw `DROP …` / `RENAME …` / `SET NOT NULL` SQL) is forbidden in `upgrade()`.

The CI script `scripts/ci/check_migration_compat.py` enforces this at the AST
level. Production **never** runs `alembic downgrade` — rollback is
image-revert plus forward-compatible schema discipline (`helm rollback`
contract, Goal #11 DoD). The additive-only rule is what makes that safe: any
older image can read any newer schema. See the rollback contract below.

## Rollback contract (readiness ↔ migrations, #1607)

The chart applies migrations from a `helm.sh/hook: pre-install,pre-upgrade`
Job (`deploy/charts/meho/templates/migration-job.yaml`), so the schema moves
**before** the Deployment rolls. Helm's `helm rollback` / `--atomic` reverts
**manifests only** — hook side effects such as a committed migration survive
(documented Helm limitation; hook resources are not tracked as part of the
release). After any rollback across a migration the database is therefore
*ahead* of the running image, by design.

Two enforced invariants make that state safe instead of an outage:

1. **Additive-only `upgrade()`** (above, CI-gated by
   `scripts/ci/check_migration_compat.py` via `migration-compat.yml`) — the
   older code never depends on schema objects the newer migration removed,
   because nothing is removed.
2. **DB-ahead-tolerant readiness** — `db_migration_probe`
   (`backend/src/meho_backplane/db/migrations.py`) reports `ok=True` with
   detail `current=<newer> head=<older> db_ahead=true` when the DB's
   revision does not resolve in the image's `versions/` directory (only a
   newer release's migration Job can have stamped it). A revision the image
   *does* know that differs from head means the DB is **behind** — that
   still fails readiness, as does a fresh unmigrated DB.

Before #1607 the probe demanded strict `current == head` equality, which
poisoned the deploy lifecycle twice over (live 2026-06-08, v0.12.0): the
moment the pre-upgrade Job committed `0037`, the *still-running* prior pods
flipped NotReady (their head was `0036`), and after `--atomic` rolled the
failed release back, the restored pods could never become Ready either —
the service could move neither forward nor back without manual `alembic`.

**Why not a `pre-rollback` downgrade hook** (the textbook Helm answer):
Helm renders rollback hooks from the *previous* release's stored manifests
(`pkg/action/rollback.go` builds the target release with
`Hooks: previousRelease.Hooks`), i.e. the **old image** — which does not
ship the newer migration scripts. `alembic downgrade` must load the script
of every revision it walks down through, so it dies with `Can't locate
revision identified by '0037'` (verified against Alembic 1.18.4) and the
failed hook would block the rollback outright. Even if the scripts were
available, an unattended downgrade is destructive: `0037`'s `downgrade()`
drops `doc_collections`, destroying anything written during the
failed-release window. Automatic downgrades stay banned.

**Trade-offs, accepted in #1607:**

- The probe no longer flags "DB at a revision this image does not know" as
  a failure, so a corrupted or foreign `alembic_version` value passes
  readiness. The dangerous direction (DB behind the code — expected tables
  or columns missing) still fails closed, and the tolerated state stays
  visible on `/ready` as `db_ahead=true`.
- The tolerance ships *in the image being rolled back to*. Rolling back to
  a pre-tolerance image (strict-equality probe) across a migration still
  bricks readiness — recover by rolling forward.

`downgrade()` bodies remain **mandatory and real** (see `0037`): they are
the development-time symmetry check and the *manual*, operator-driven
escape hatch — never part of the automated deploy lifecycle.

What re-arms the trap: a migration that smuggles destructive DDL past the
compat gate, or a readiness check that reintroduces strict revision
equality. Both are CI/test-pinned (`test_migration_compat.py`,
`test_alembic_probe.py`).

## Async and sync engine interaction

`backend/alembic/env.py` provides an async runner that wraps `upgrade()` and
`downgrade()` in `asyncio.run()`. Tests that call `alembic.command.upgrade()`
must therefore be synchronous (`def test_...`, not `async def test_...`) to
avoid "event loop already running" errors.

## References

- Task #1607: pre-upgrade hook ↔ auto-rollback trap; db-ahead-tolerant
  readiness probe (G0.22, 2026-06-08 outage).
- Helm chart hooks (hook resources unmanaged by the release):
  <https://helm.sh/docs/topics/charts_hooks/>; `--atomic` rolls back
  manifests only, not hook side effects:
  <https://github.com/helm/helm/issues/7158>.
- PR #1045: the UUID `str()` vs `.hex` incident (G7.1-T5).
- Task #1095: UUID audit + drift-guard (G0.11 CI hardening).
- `backend/alembic/versions/0018_seed_rdc_internal_conventions.py`: the
  canonical data-migration reference implementation with `_uuid_param` and
  `_coerce_uuid` helpers.
- `backend/alembic/versions/0038_backfill_endpoint_descriptor_product_splits.py`:
  the canonical collision-guarded backfill-rewrite (correlated `NOT EXISTS`
  twin check on a partial-unique natural key, documented no-op downgrade).
- `backend/tests/migrations/test_alembic_uuid_param_consistency.py`: drift-guard test.
- `backend/tests/migrations/test_alembic_seed_rdc_conventions.py`: 0018 behavioural tests.
- `backend/tests/migrations/test_migration_compat.py`: additive-only CI guard tests.

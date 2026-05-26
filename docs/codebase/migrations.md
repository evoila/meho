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

`backend/tests/test_alembic_uuid_param_consistency.py` contains an AST-based
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
6. **Reversible.** `downgrade()` must undo the data (delete seeded rows by a
   stable discriminant such as `actor_sub` or slug) without touching rows the
   operator authored independently.

## Additive-only constraint

Migrations in `upgrade()` must be additive: `create_table`, `add_column`,
`create_index`, `create_foreign_key`, or data backfills. Destructive DDL
(`drop_column`, `drop_table`, `rename_table`, `alter_column nullable=False`,
raw `DROP …` / `RENAME …` / `SET NOT NULL` SQL) is forbidden in `upgrade()`.

The CI script `scripts/ci/check_migration_compat.py` enforces this at the AST
level. Running `alembic downgrade` in production is the rollback path; the
schema must survive a rollback without manual DB intervention (`helm rollback`
contract, Goal #11 DoD).

## Async and sync engine interaction

`backend/alembic/env.py` provides an async runner that wraps `upgrade()` and
`downgrade()` in `asyncio.run()`. Tests that call `alembic.command.upgrade()`
must therefore be synchronous (`def test_...`, not `async def test_...`) to
avoid "event loop already running" errors.

## References

- PR #1045: the UUID `str()` vs `.hex` incident (G7.1-T5).
- Task #1095: UUID audit + drift-guard (G0.11 CI hardening).
- `backend/alembic/versions/0018_seed_rdc_internal_conventions.py`: the
  canonical data-migration reference implementation with `_uuid_param` and
  `_coerce_uuid` helpers.
- `backend/tests/test_alembic_uuid_param_consistency.py`: drift-guard test.
- `backend/tests/test_alembic_seed_rdc_conventions.py`: 0018 behavioural tests.
- `backend/tests/test_migration_compat.py`: additive-only CI guard tests.

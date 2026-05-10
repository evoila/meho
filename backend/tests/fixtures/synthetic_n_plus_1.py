# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthetic N+1 additive migration applied during the forward-compat test.

Goal #11's DoD bullet 3 ("``helm rollback`` works without DB
intervention") imposes two disciplines:

* Migration-side — every ``upgrade()`` is purely additive; the CI
  guard at ``scripts/ci/check_migration_compat.py`` enforces this.
* Code-side — the running backplane image must tolerate a schema
  that is *ahead* of it (the situation a rollback lands in: image
  reverted to revision N, schema still at revision N+1).

This module owns the second discipline's test fixture: a small
helper that drops two additive columns onto ``audit_log`` via raw
DDL, simulating an N+1 additive migration without going through
Alembic. The helper is intentionally **not** a real Alembic
migration file:

* A real revision under ``backend/alembic/versions/`` would be picked
  up by the script-directory walker on the next ``alembic upgrade
  head``, polluting the production migration sequence in a way that
  would be invisible from the test alone.
* A real revision would also be scanned by the CI guard's path
  filter (``backend/alembic/versions/**``) and would have to pass
  the destructive-pattern check — which the synthetic *should* pass
  (it is purely additive), but the contract-test value is in keeping
  the synthetic clearly out-of-band from production migrations.

The two columns added match the issue body verbatim:

* ``future_field text NULL DEFAULT 'reserved_for_v0.2'`` — proves
  the simple-text/string default path.
* ``future_jsonb_field jsonb NULL DEFAULT '{}'::jsonb`` — proves the
  PG-specific JSONB default path; this is the realistic shape future
  v0.2 schema additions will take (the ``payload`` column on
  ``audit_log`` already uses JSONB, so a sibling JSONB column is a
  representative additive change).

Both defaults are applied **server-side** by PostgreSQL itself when
the revision-N backplane code inserts an ``audit_log`` row without
mentioning the new columns. Asserting the defaults landed on the
written row is the negative test for "revision-N code did not write
the future columns" — the load-bearing forward-compat property.

The DDL runs through an ``asyncpg``-backed :class:`AsyncEngine`
because that is the only PostgreSQL driver installed in the
backplane's environment (``pyproject.toml`` pins ``asyncpg>=0.29``;
no ``psycopg`` / ``psycopg2`` is shipped — see ADR 0004). Calling
this helper from a sync test wrapper means the helper itself owns
the ``asyncio.run`` boundary so the caller does not need to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

__all__ = [
    "FUTURE_JSONB_FIELD_DEFAULT",
    "FUTURE_TEXT_FIELD_DEFAULT",
    "SYNTHETIC_N_PLUS_1_COLUMNS",
    "apply_synthetic_n_plus_1_migration",
]

#: Default value PostgreSQL applies to ``future_field`` rows whose
#: INSERT does not mention the column. Pinned as a module constant so
#: the test asserts on the same string the migration writes — drift in
#: either direction surfaces as a failed test rather than a silent
#: assertion miss.
FUTURE_TEXT_FIELD_DEFAULT: Final[str] = "reserved_for_v0.2"

#: Default value PostgreSQL applies to ``future_jsonb_field``. The
#: column type is ``jsonb`` so the value round-trips as a Python
#: ``dict`` through asyncpg / SQLAlchemy 2.x; the constant is kept as
#: the dict (not the SQL literal) so test assertions compare apples
#: to apples without re-parsing the JSON.
FUTURE_JSONB_FIELD_DEFAULT: Final[dict[str, object]] = {}

#: The columns this synthetic migration adds, in the order they are
#: declared inside :func:`apply_synthetic_n_plus_1_migration`. Exposed
#: as a tuple so a test can iterate over expected names without
#: re-stating them inline. Kept out of the function body to keep the
#: DDL strings as the only place column names appear in the SQL itself.
SYNTHETIC_N_PLUS_1_COLUMNS: Final[Sequence[str]] = (
    "future_field",
    "future_jsonb_field",
)


async def _run_alters(async_url: str) -> None:
    """Open an asyncpg engine and issue the two ``ALTER TABLE`` DDL stmts.

    Two distinct statements rather than one combined ``ADD COLUMN
    ..., ADD COLUMN ...`` so a failure localises to the offending
    column instead of rolling both back as a unit. Both columns are
    NULLABLE with server-side defaults; PostgreSQL applies the
    default to existing rows lazily (since 11), so this is O(1)
    regardless of ``audit_log`` row count.
    """
    engine = create_async_engine(async_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE audit_log "
                    "ADD COLUMN future_field text NULL "
                    f"DEFAULT '{FUTURE_TEXT_FIELD_DEFAULT}'"
                ),
            )
            await conn.execute(
                text(
                    "ALTER TABLE audit_log "
                    "ADD COLUMN future_jsonb_field jsonb NULL "
                    "DEFAULT '{}'::jsonb"
                ),
            )
    finally:
        await engine.dispose()


def apply_synthetic_n_plus_1_migration(async_url: str) -> None:
    """Apply the N+1 additive migration to ``audit_log``.

    *async_url* must be an async PostgreSQL URL
    (``postgresql+asyncpg://...``) — the only driver shipped in the
    backplane environment. The helper drives its own
    :func:`asyncio.run` so the caller can stay synchronous; this
    matches the pattern Alembic's ``command.upgrade`` uses internally
    via the env.py async cookbook (see
    ``backend/alembic/env.py``'s ``run_migrations_online``). Calling
    this from inside a running event loop would crash with
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` — by design; the forward-compat test runs
    synchronously precisely so this nesting is impossible.

    Returns ``None``; callers that need to inspect the columns
    afterwards re-issue their own ``information_schema`` /
    ``inspect(engine)`` query.
    """
    asyncio.run(_run_alters(async_url))

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reap epoch-versioned connector registrations (#2977).

Revision ID: 0075
Revises: 0074
Create Date: 2026-08-19

Task #2977 under Initiative #3020 (G0.40 hardening wave 2). The consumer
catalog on a v0.28.0 deploy accumulated 9-10 stale
``fleet-rest-probe-<epoch>`` connector rows that grew one-per-probe-run
with nothing to reap them. Mechanism (per the issue's grounding pass):
the probe-then-ingest glue on the consumer side stamped the probe run's
**Unix epoch as the connector ``version``**, so every run rendered a
fresh ``(product, version, impl_id)`` triple. The ingest upsert
(:mod:`meho_backplane.operations.ingest._upsert`) dedups on that exact
triple, so a changed ``version`` never matched an existing row — it
INSERTed a brand-new connector each time, and no TTL / reaper / GC
exists anywhere for ``endpoint_descriptor`` / ``operation_group`` rows.

The paired ingest-time guard
(:meth:`meho_backplane.operations.ingest.api_schemas.IngestRequest._reject_epoch_version`)
now rejects an epoch ``version`` at the source on every surface
(REST / MCP / CLI all construct the same ``IngestRequest``), so this
class cannot recur. This migration is the one-time cleanup of the rows
that already leaked in before that guard existed.

What counts as an "epoch version"
---------------------------------

A bare integer of at least 9 digits. Unix-epoch **seconds** have been 10
digits since 2001-09 (crossed 1e9) and stay 10 until 2286; a 9-digit
floor catches any epoch while never matching a real connector version,
which always carries dots / letters / dashes (``9.0`` / ``2.x`` /
``2026-04``). This is the same rule the ingest-time guard applies, so the
prevention and the cleanup target one identical shape. The bundled
``net-probe-1.x`` typed connector (``version="1.x"``) and every ingested
connector with a genuine product-line version are untouched.

Tenant-scope-inclusive (the #2977 sharpening)
---------------------------------------------

Unlike migrations ``0049`` / ``0052`` (which retire *built-in*
``tenant_id IS NULL`` product-spelling twins only), the epoch rows are
consumer-side **tenant** ingests, so this cleanup deliberately spans
**every** scope — it does not filter on ``tenant_id``. An epoch version
is illegitimate regardless of scope, so a blanket predicate is correct
and there is no tenant row worth preserving.

``endpoint_descriptor`` additionally guards on ``source_kind =
'ingested'`` — typed / composite connectors are hand-coded and can never
carry an epoch version, but the guard makes the blast radius provably
ingest-only. ``operation_group`` has no ``source_kind`` column; the
epoch-version predicate alone is diagnostic there (no legitimate group
carries one). Descriptors are retired before groups so the two row
surfaces the dispatch / review layer reads are cleared together, same
ordering as ``0049`` / ``0052``.

Portability
-----------

The "bare integer >= 9 digits" test is dialect-branched: PostgreSQL uses
a POSIX regex (``version ~ '^[0-9]{9,}$'``); SQLite — which has no regex
operator without a registered function — expresses the identical set as
"length >= 9 AND no non-digit character" via ``length()`` + ``NOT
GLOB``. Both run inside the single migration transaction against the
operator-scale catalog. No UUID bind params are threaded (the predicate
keys only on the ``Text`` ``version`` / ``source_kind`` columns), so the
``docs/codebase/migrations.md`` UUID-hex gotcha does not apply.

Additive-only forward, no-op back
---------------------------------

``upgrade()`` runs no DDL — only two DML DELETEs — so it clears
``scripts/ci/check_migration_compat.py`` (which bans destructive DDL,
not row deletion) and an older image reading the post-cleanup schema is
unaffected (the ``helm rollback`` = image-revert + forward-compat-schema
contract, ``docs/codebase/migrations.md``, holds). ``downgrade()`` is a
documented no-op: the reaped rows were non-idempotent debris that no
dispatch / review probe wanted, and re-INSERTing fabricated epoch rows
would re-introduce the unbounded growth this migration removes. Same
shape as ``0049`` / ``0052``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _epoch_version_predicate(dialect_name: str) -> sa.sql.elements.TextClause:
    """Return the dialect-portable "``version`` is a Unix-epoch integer" WHERE.

    A bare integer of at least 9 digits. PostgreSQL uses a POSIX regex;
    SQLite (no regex operator without a registered function) expresses
    the same set as "length >= 9 AND no non-digit character present".
    Both reference the bare ``version`` column, which resolves against
    the single table of each ``DELETE`` this predicate is attached to.
    """
    if dialect_name == "postgresql":
        return sa.text("version ~ '^[0-9]{9,}$'")
    return sa.text("length(version) >= 9 AND version NOT GLOB '*[^0-9]*'")


def upgrade() -> None:
    """Delete every epoch-versioned ingested descriptor + group row, any scope."""
    bind = op.get_bind()
    epoch = _epoch_version_predicate(bind.dialect.name)

    descriptor = sa.table(
        "endpoint_descriptor",
        sa.column("source_kind", sa.Text()),
        sa.column("version", sa.Text()),
    )
    group = sa.table(
        "operation_group",
        sa.column("version", sa.Text()),
    )

    bind.execute(
        sa.delete(descriptor).where(
            descriptor.c.source_kind == "ingested",
            epoch,
        )
    )
    bind.execute(sa.delete(group).where(epoch))


def downgrade() -> None:
    """No-op by design.

    The reaped rows were non-idempotent probe debris (a fresh connector
    per probe run, the defect #2977 fixes) that no dispatch / review
    probe wanted; re-INSERTing fabricated epoch rows would re-introduce
    the unbounded growth. No DDL runs in ``upgrade()``, so there is
    nothing to undo at the schema layer either. Same documented-no-op
    shape as ``0049`` / ``0052``. The function stays defined so
    ``alembic downgrade -1`` resolves the symbol cleanly.
    """
    # Intentionally empty -- see docstring.

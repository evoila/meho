# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reap probe-epoch-impl_id connector registrations (#3061).

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-31

Task #3061, follow-up to #2977 (Initiative #3020, G0.40 hardening). Sibling
of ``0075_reap_epoch_versioned_connector_registrations``, which reaped the
same class of debris keyed on the **wrong column**.

The defect ``0075`` targeted
---------------------------

A consumer-side probe-then-ingest loop mints a *fresh* connector per probe
run, so the catalog grows one row per run with nothing to reap it (the
ingest upsert dedups on the ``(product, version, impl_id)`` triple, so a
changed key never matches an existing row — it INSERTs). ``0075`` closed
this by reaping rows whose **``version``** is a bare Unix-epoch integer,
and the paired ``IngestRequest._reject_epoch_version`` guard rejects that
shape at registration.

Why ``0075`` missed the rows still on the lab
---------------------------------------------

The rows actually accreting carry the epoch in the **``impl_id``**, not the
``version``: ``impl_id="fleet-rest-probe-<epoch>"`` with a *legitimate*
``version="9.0"`` (rendering ``connector_id="fleet-rest-probe-<epoch>-9.0"``).
``0075``'s predicate (``version ~ '^[0-9]{9,}$'``) never matched — ``"9.0"``
is not an epoch — so a v0.29.0 lab deploy that had applied ``0075`` still
returned 9 ``fleet-rest-probe-<epoch>-9.0`` rows post-migration (epochs
``1784123249 .. 1786183253``, Jul→Aug 2026). #2977's own body named this
exact shape — *"reap ``<impl>-probe-<epoch>`` rows"* — but the shipped
predicate looked for ``<impl>-<epoch>`` (epoch-as-version).

What this migration reaps
-------------------------

``endpoint_descriptor`` (guarded ``source_kind = 'ingested'``) and
``operation_group`` rows whose **``impl_id``** ends in ``-probe-<epoch>``:
a ``-probe-`` segment followed by a bare integer of at least 9 digits at
the end of the string. A 9-digit floor catches any Unix epoch (10 digits
since 2001-09, staying 10 until 2286) while the ``-probe-`` anchor and the
end-of-string digit run keep it off every legitimate connector: the stable
probe impls end in a bare ``-probe`` with no trailing epoch
(``net-probe`` / ``nsx-rest-probe`` / ``sddc-rest-probe`` /
``vmware-rest-probe`` / ``fleet-rest-probe``), and every real product-line
impl_id carries no ``-probe-<digits>`` tail at all
(``fleet-rest`` / ``vmware-rest`` / ``vcd`` / ``vrops8``). The paired
registration-time guard
(:meth:`meho_backplane.operations.ingest.api_schemas.IngestRequest._reject_probe_epoch_impl_id`)
rejects the same shape on every ingest surface so the class cannot recur;
this migration is the one-time cleanup of the rows that leaked in before it.

Complements, not replaces, ``0075``: an epoch can live in either column
(``version`` — ``0075``; ``impl_id`` tail — this one), and the two
predicates target disjoint row sets. Both keep ``0075``'s properties: the
cleanup is **tenant-scope-inclusive** (the debris is a consumer-side
tenant ingest, illegitimate at any scope, so no ``tenant_id`` filter);
``endpoint_descriptor`` additionally guards ``source_kind = 'ingested'``
(typed/composite connectors are hand-coded and never carry a probe-epoch
impl_id, but the guard makes the blast radius provably ingest-only);
``operation_group`` has no ``source_kind`` column so the impl_id predicate
alone is diagnostic there. Descriptors are retired before groups, same
ordering as ``0049`` / ``0052`` / ``0075``.

Portability
-----------

The predicate is dialect-branched. PostgreSQL uses a POSIX regex
(``impl_id ~ '-probe-[0-9]{9,}$'``). SQLite — which has no regex operator
without a registered function — expresses the identical set as a ``GLOB``
requiring a ``-probe-`` segment followed by at least nine digits, ANDed
with "the string ends in a digit" (``NOT GLOB '*[^0-9]'``) so the epoch
must run to the end exactly as the PG ``$`` anchor demands. No UUID bind
params are threaded (the predicate keys only on the ``Text`` ``impl_id`` /
``source_kind`` columns), so the ``docs/codebase/migrations.md`` UUID-hex
gotcha does not apply.

Additive-only forward, no-op back
---------------------------------

``upgrade()`` runs no DDL — only two DML DELETEs — so it clears
``scripts/ci/check_migration_compat.py`` (which bans destructive DDL, not
row deletion) and an older image reading the post-cleanup schema is
unaffected. ``downgrade()`` is a documented no-op: the reaped rows were
non-idempotent probe debris no dispatch / review probe wanted, and
re-INSERTing fabricated epoch rows would re-introduce the unbounded growth
this migration removes. Same shape as ``0049`` / ``0052`` / ``0075``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _probe_epoch_impl_id_predicate(dialect_name: str) -> sa.sql.elements.TextClause:
    """Return the dialect-portable "``impl_id`` ends in ``-probe-<epoch>``" WHERE.

    A ``-probe-`` segment followed by a bare integer of at least 9 digits
    running to the end of the string. PostgreSQL uses a POSIX regex;
    SQLite (no regex operator without a registered function) expresses the
    same set as a ``GLOB`` for ``-probe-`` + nine digits, ANDed with "ends
    in a digit" so the digit run reaches the end exactly as the PG ``$``
    anchor demands. Both reference the bare ``impl_id`` column, which
    resolves against the single table of each ``DELETE`` this predicate is
    attached to.
    """
    if dialect_name == "postgresql":
        return sa.text("impl_id ~ '-probe-[0-9]{9,}$'")
    return sa.text(
        "impl_id GLOB '*-probe-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*' "
        "AND impl_id NOT GLOB '*[^0-9]'"
    )


def upgrade() -> None:
    """Delete every probe-epoch-impl_id ingested descriptor + group row, any scope."""
    bind = op.get_bind()
    probe_epoch = _probe_epoch_impl_id_predicate(bind.dialect.name)

    descriptor = sa.table(
        "endpoint_descriptor",
        sa.column("source_kind", sa.Text()),
        sa.column("impl_id", sa.Text()),
    )
    group = sa.table(
        "operation_group",
        sa.column("impl_id", sa.Text()),
    )

    bind.execute(
        sa.delete(descriptor).where(
            descriptor.c.source_kind == "ingested",
            probe_epoch,
        )
    )
    bind.execute(sa.delete(group).where(probe_epoch))


def downgrade() -> None:
    """No-op by design.

    The reaped rows were non-idempotent probe debris (a fresh connector
    per probe run, the defect #3061 fixes) that no dispatch / review probe
    wanted; re-INSERTing fabricated epoch rows would re-introduce the
    unbounded growth. No DDL runs in ``upgrade()``, so there is nothing to
    undo at the schema layer either. Same documented-no-op shape as
    ``0049`` / ``0052`` / ``0075``. The function stays defined so
    ``alembic downgrade -1`` resolves the symbol cleanly.
    """
    # Intentionally empty -- see docstring.

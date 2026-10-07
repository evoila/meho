# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let the ``raw_payload`` age-off pass the ``audit_log`` append-only guard.

Revision ID: 0103
Revises: 0102
Create Date: 2026-10-06

The problem
-----------

Two earlier changes did not fit together on PostgreSQL:

* Migration ``0100`` installs the trigger ``audit_log_append_only``. It runs
  ``audit_log_reject_mutation()`` before every ``UPDATE`` or ``DELETE`` on
  ``audit_log`` and rejects it, under every role.
* :mod:`meho_backplane.audit_retention` runs a weekly
  ``UPDATE audit_log SET raw_payload = NULL WHERE occurred_at < cutoff AND
  raw_payload IS NOT NULL``. ``raw_payload`` is the connector's answer
  *before* redaction, so it can hold secrets.

The trigger rejected that ``UPDATE`` on every run. So on PostgreSQL
``raw_payload`` was never aged off. The SQLite test lane has no trigger,
so its tests did not see the problem.

What this migration changes (PostgreSQL only)
---------------------------------------------

It replaces the function ``audit_log_reject_mutation()``. The trigger itself
(name, timing, events) stays as ``0100`` created it.

* ``DELETE`` is still always rejected.
* An ``UPDATE`` is allowed **only** when it sets ``raw_payload`` from a value
  to SQL ``NULL`` and leaves every other stored byte of the row unchanged.
  Every other ``UPDATE`` is rejected with the same error text as ``0100``.

This holds for any row of any age, and for any role that has ``UPDATE`` on
``audit_log``: such a role can empty ``raw_payload``, but it cannot change
anything else in the row or delete it.

How the check works
-------------------

The function builds ``expected``: the ``OLD`` row with only ``raw_payload``
set to ``NULL``. It then compares ``NEW`` with ``expected`` using
``*=`` (``record_image_eq``). That operator compares the stored bytes of
every column, and treats two ``NULL`` values as equal. So:

* A value that means the same but is stored differently is a change and is
  rejected. Examples: ``json`` key order, spaces, a duplicate key, ``1`` vs
  ``1.000``, ``jsonb`` ``1.0`` vs ``1.00``, SQL ``NULL`` vs JSON ``null``.
* ``raw_payload`` is never parsed. A ``json`` value that ``jsonb`` cannot
  hold (for example ``\\u0000``) does not break the check or the weekly run.
* A column added later is covered without changing this function. One
  exception: a ``STORED`` generated column is still empty in ``NEW`` when a
  ``BEFORE`` trigger runs, so it would make the age-off fail (closed, not
  open) until this function is changed.

The function pins ``search_path = pg_catalog, pg_temp`` and names its
operators and types with the ``pg_catalog`` schema. So a caller cannot
change what the check does by putting its own functions or operators (for
example a fake ``to_jsonb(audit_log)``) on its ``search_path``. Like ``0100``
it is not ``SECURITY DEFINER``: it runs with the caller's rights.

What this does not stop
-----------------------

A role that owns ``audit_log`` (or a superuser) can still disable the
trigger, replace this function, or ``TRUNCATE`` the table. That was already
true with ``0100``. The trigger stops mistakes and roles that do not own the
table.

Dialect guard
-------------

Same as ``0100``: plpgsql cannot run on the SQLite dev/test lane, so
``upgrade()`` and ``downgrade()`` are a no-op there.

Reversibility contract
----------------------

``downgrade()`` puts back the exact ``0100`` function (reject every
``UPDATE`` and ``DELETE``). ``CREATE OR REPLACE FUNCTION`` resets every
setting it does not name, so the pinned ``search_path`` goes away too. The
trigger is not touched in either direction.
``CREATE OR REPLACE FUNCTION`` is not on the migration compat guard's
banned list, and an older image only ever ``INSERT``s, so the rollback
contract holds.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0103"
down_revision: str | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow only the ``raw_payload`` -> NULL update through the guard."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            expected pg_catalog.record;
        BEGIN
            -- Nested IF: NEW is only read on UPDATE (it is NULL on DELETE).
            IF TG_OP OPERATOR(pg_catalog.=) 'UPDATE' THEN
                IF OLD.raw_payload IS NOT NULL AND NEW.raw_payload IS NULL THEN
                    -- The row as the age-off must leave it: OLD with only
                    -- raw_payload emptied.
                    expected := OLD;
                    expected.raw_payload := NULL;
                    -- *= compares the stored bytes of every column (two
                    -- NULLs are equal). It never parses json, and a value
                    -- that means the same but is stored differently counts
                    -- as a change.
                    IF NEW OPERATOR(pg_catalog.*=) expected THEN
                        RETURN NEW;
                    END IF;
                END IF;
            END IF;
            RAISE EXCEPTION
                'audit_log is append-only: % is not permitted '
                '(governance invariant, v0.1-spec section 6)', TG_OP;
        END;
        $$;
        """
    )


def downgrade() -> None:
    """Restore the strict ``0100`` function: reject every UPDATE and DELETE."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only: % is not permitted '
                '(governance invariant, v0.1-spec section 6)', TG_OP;
        END;
        $$;
        """
    )

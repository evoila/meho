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

It replaces the body of ``audit_log_reject_mutation()``. The trigger itself
(name, timing, events) stays as ``0100`` created it.

* ``DELETE`` is still always rejected.
* An ``UPDATE`` is allowed **only** when it does nothing except empty
  ``raw_payload``: the old value is not SQL ``NULL``, the new value is SQL
  ``NULL``, and every other column is unchanged. "Every other column" is
  checked as ``to_jsonb(NEW) - 'raw_payload' = to_jsonb(OLD) - 'raw_payload'``,
  so a column added later is covered without touching this function.
* Every other ``UPDATE`` is rejected with the same error text as ``0100``.

The record of account (``payload``, ``status_code``, ``operator_sub``, the
redaction manifest and the rest of the row) therefore stays immutable. The
only change the datastore accepts is the one-way removal of the
pre-redaction body.

Dialect guard
-------------

Same as ``0100``: plpgsql cannot run on the SQLite dev/test lane, so
``upgrade()`` and ``downgrade()`` are a no-op there.

Reversibility contract
----------------------

``downgrade()`` puts back the exact ``0100`` function body (reject every
``UPDATE`` and ``DELETE``). The trigger is not touched in either direction.
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
        AS $$
        BEGIN
            -- Nested IF: NEW is only read on UPDATE (it is NULL on DELETE),
            -- whatever order the inner conditions are evaluated in.
            IF TG_OP = 'UPDATE' THEN
                IF OLD.raw_payload IS NOT NULL
                   AND NEW.raw_payload IS NULL
                   AND (to_jsonb(NEW) - 'raw_payload') = (to_jsonb(OLD) - 'raw_payload')
                THEN
                    RETURN NEW;
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

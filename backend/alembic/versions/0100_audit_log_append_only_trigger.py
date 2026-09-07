# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Enforce ``audit_log`` append-only at the datastore (security S18).

Revision ID: 0100
Revises: 0099
Create Date: 2026-09-07

CLAUDE.md postulate 7 (v0.1-spec section 6) makes the audit trail
synchronous, append-only and tamper-evident -- the load-bearing property
of the whole governance model. Until this migration that property was a
**code convention only**: the write path is INSERT-only and no code path
issues an ``UPDATE`` / ``DELETE`` against ``audit_log``, but nothing at the
datastore stopped one. Anyone with write access to the Postgres instance
(the app DB role, a DB operator, or in-process code execution) could
silently rewrite or erase the audit row of the operation just performed --
the free cover-up a compromise would otherwise inherit.

Security review 2026-09-06 finding S18 (Task meho-internal#306 under
Initiative #262). Defence-in-depth: an invariant the application depends on
belongs at the datastore that owns the data, not scattered as convention.

What this migration adds (PostgreSQL only)
------------------------------------------

* ``audit_log_reject_mutation()`` -- a ``plpgsql`` trigger function that
  ``RAISE``s on any row it fires for. ``TG_OP`` names the rejected
  operation in the error message.
* ``audit_log_append_only`` -- a ``BEFORE UPDATE OR DELETE ... FOR EACH
  ROW`` trigger that calls the function. ``INSERT`` and ``SELECT`` are
  untouched, so the synchronous audit write path keeps working; a direct
  ``UPDATE`` or ``DELETE`` fails at the datastore under **every** role,
  including the app role and a DB superuser (a row-level trigger fires
  regardless of privilege).

Dialect guard
-------------

The enforcement is a production-datastore guarantee, so the DDL runs only
on ``postgresql``. The SQLite dev/test lane (aiosqlite) cannot execute
plpgsql trigger DDL, and the unit lanes assert the app-level convention
rather than the datastore trigger; ``upgrade()`` / ``downgrade()`` are a
clean no-op there. This keeps the SQLite ``alembic upgrade head`` smokes in
``tests/test_db_models.py`` green.

Out of scope (tracked separately under #262)
--------------------------------------------

* Retention / archival ``DELETE`` of aged rows. No retention job exists
  today (a codebase grep finds only ``select(AuditLog)`` reads). When one
  lands it drops whole **partitions** -- ``DROP TABLE`` on a partition and
  ``TRUNCATE`` both bypass row-level triggers -- or runs under a dedicated
  retention role, so this row-level trigger does not need a bypass hatch.
* Cryptographic tamper-evidence (hash-chaining / signed rows) is a stronger
  model than immutability and is not this change.

Reversibility contract
----------------------

``downgrade()`` drops the trigger then the function (reverse of create
order), Postgres only. Additive on PostgreSQL (a new trigger + function, no
ALTER of an existing object); a no-op on SQLite.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0100"
down_revision: str | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Install the append-only trigger + function on PostgreSQL."""
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
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION audit_log_reject_mutation();
        """
    )


def downgrade() -> None:
    """Drop the trigger then the function (PostgreSQL only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_mutation()")

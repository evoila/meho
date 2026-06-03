# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``ON DELETE CASCADE`` to ``scheduled_trigger.agent_definition_id``.

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-03

Issue #1480 (G0.19 v0.10.0 dogfood hardening). Migration ``0020`` (T1,
PR #1064) created ``scheduled_trigger`` with a real
``REFERENCES agent_definition(id)`` FK on ``agent_definition_id`` but no
``ondelete`` clause -- so the constraint defaults to ``NO ACTION``.
Deleting an ``agent_definition`` that ever had a trigger created against
it (including a now-**cancelled** one, since
:meth:`SchedulerService.cancel` retains the row for audit) violates that
FK. The delete path issues a bulk Core ``DELETE`` whose uncaught
:class:`~sqlalchemy.exc.IntegrityError` surfaced as a bare JSON-RPC
``-32603 "internal error: IntegrityError"`` on MCP and an unhandled
HTTP 500 on REST -- the definition was effectively undeletable via the
API.

Chosen shape: DB-level ``ON DELETE CASCADE``
--------------------------------------------

The delete is a **bulk Core statement** (``DELETE ... RETURNING name``),
not a ``Session.delete``, so an ORM ``cascade=`` / ``passive_deletes``
relationship would not fire (SQLAlchemy 2.0 ``cascade`` applies only to
unit-of-work deletes; a database-level ``ON DELETE`` fires on bulk/Core
statements). The cascade therefore lives in the FK clause itself.

``CASCADE`` rather than ``SET NULL`` because ``agent_definition_id`` is
``NOT NULL`` -- nulling it would break the column contract and the
scheduler's "fire runs against this definition" invariant. There is no
operator API path that hard-deletes a trigger row (``cancel()`` keeps it
for audit), so a guard-with-typed-error shape would leave a
once-scheduled definition permanently undeletable. ``scheduled_trigger``
is the **only** hard FK to ``agent_definition`` (``agent_run`` is a
nullable soft-FK with no ``ForeignKey`` clause, ``audit_log`` is a
separate table) so the cascade is bounded to a single child table:
deleting a definition removes the definition and its dependent schedule
rows, while run history and audit logs survive.

Portability: dialect-split FK rebuild
--------------------------------------

* **PostgreSQL** -- ``batch_alter_table`` runs online ``ALTER``
  statements on PG, so we drop the server-generated FK by its
  deterministic default name
  (``scheduled_trigger_agent_definition_id_fkey``) and re-add it with
  ``ondelete='CASCADE'``. No table recreate.
* **SQLite** -- the FK was created unnamed; SQLite cannot
  ``ALTER ... DROP CONSTRAINT`` a foreign key, so ``batch_alter_table``
  performs the move-and-copy table recreate. A ``naming_convention`` is
  passed so the existing FK reflects under a deterministic name we can
  drop, and the FK is re-added with the cascade. This mirrors the
  table-recreate cookbook the status-CHECK migrations (0017 / 0025)
  already rely on.

Reversibility contract
----------------------

``downgrade()`` rebuilds the FK back to the no-``ondelete`` (``NO
ACTION``) shape that 0020 shipped, on both dialects, so the two halves
are symmetric.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Deterministic name for the ``agent_definition_id`` FK.
#:
#: On PostgreSQL the constraint 0020 created carries the dialect-default
#: name ``<table>_<column>_fkey``; we drop that exact name. On SQLite the
#: FK is unnamed, so we hand ``batch_alter_table`` a ``naming_convention``
#: that renders this same name on reflection, letting the drop /
#: re-create reference a stable handle across both dialects.
_FK_NAME = "scheduled_trigger_agent_definition_id_fkey"

#: ``naming_convention`` whose ``fk`` template renders ``_FK_NAME`` for the
#: ``(scheduled_trigger, agent_definition_id, agent_definition)`` triple,
#: matching the PostgreSQL server-generated default so both dialects drop
#: the same name.
_NAMING_CONVENTION = {
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
}


def _rebuild_fk(*, ondelete: str | None) -> None:
    """Drop and re-create the ``agent_definition_id`` FK with *ondelete*.

    Dialect-split: PostgreSQL drops the server-named FK in place
    (online ALTER); SQLite recreates the table via ``batch_alter_table``
    with the naming convention that gives the unnamed FK a stable handle.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.drop_constraint(_FK_NAME, "scheduled_trigger", type_="foreignkey")
        op.create_foreign_key(
            _FK_NAME,
            "scheduled_trigger",
            "agent_definition",
            ["agent_definition_id"],
            ["id"],
            ondelete=ondelete,
        )
        return

    with op.batch_alter_table(
        "scheduled_trigger",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "agent_definition",
            ["agent_definition_id"],
            ["id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    """Rebuild the FK with ``ON DELETE CASCADE``."""
    _rebuild_fk(ondelete="CASCADE")


def downgrade() -> None:
    """Restore the no-``ondelete`` (``NO ACTION``) FK 0020 shipped."""
    _rebuild_fk(ondelete=None)

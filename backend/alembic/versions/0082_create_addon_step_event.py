# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Step-event push contract: ``service_account_sub`` + ``addon_step_event`` (#3027).

The durable substrate of the add-on step-event push contract (Initiative
#2900, Task #3027): a paired add-on subscribes to a durable, resumable
stream of the step events (approval outcomes, dispatch completions) that
belong to **its own** work — replacing the at-most-once, count-trimmed
Valkey SSE feed a restart would silently lose events across.

Two additive changes, no ALTER of an existing object beyond the single new
column (migration-compat contract):

1. ``addon_pairing.service_account_sub`` — the Keycloak service-account
   user id (the OIDC ``sub`` the add-on's ``client_credentials`` tokens
   carry). NULL for pairings created before this migration. It is the join
   key that binds a produced row (``approval_request.principal_sub`` /
   ``agent_run.identity_sub`` — both the add-on's ``sub``) to its pairing,
   and binds a subscription request to its pairing (``service_account_sub
   == operator.sub``). Captured at pair time from Keycloak; a NULL row
   simply never matches (fail-closed) until it re-pairs.

2. ``addon_step_event`` — one durable row per step event delivered to a
   paired add-on. ``seq`` is a ``BIGSERIAL`` monotonic cursor (the same
   portable-serial shape ``event_outbox.event_id`` uses) so an add-on
   resumes with ``WHERE pairing_id = :p AND seq > :after ORDER BY seq``
   and never misses a committed event across its own restarts. The
   ``pairing_id`` FK is ``ON DELETE CASCADE`` so unpair (which hard-deletes
   the pairing row) takes the step-event log with it — an unpaired
   backplane stays byte-identical to a never-paired one.

Revision numbering: this branch was cut from head ``0079``, so
``down_revision`` points at ``0079`` and the chain is linear here.
Sibling wave-2 PRs claimed ``0080`` and ``0081``; this migration is filed
as ``0082`` so it does not collide on the filename. When those siblings
land first, ``down_revision`` must be re-pointed to the then-current head
(``0081``) at merge time — an additive re-point, no DDL change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0082"
# Cut from head 0079 (siblings claimed 0080/0081); re-point to the current
# head at merge time if those land first.
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # (1) The pairing -> service-account-subject join key. Nullable: rows
    # written by the #3025 pair path predate the fetch; a NULL never
    # matches a produced ``sub`` (fail-closed).
    op.add_column(
        "addon_pairing",
        sa.Column("service_account_sub", sa.Text(), nullable=True),
    )
    # Drives both the produce-time attribution lookup
    # (``service_account_sub == owner_principal_sub``) and the
    # subscription bind (``service_account_sub == operator.sub``). Plain
    # b-tree, not unique: Keycloak client uniqueness already makes the
    # service-account subject one-per-pairing, and a plain index sidesteps
    # cross-dialect partial-unique-with-NULL grammar.
    op.create_index(
        "addon_pairing_service_account_sub_idx",
        "addon_pairing",
        ["service_account_sub"],
        postgresql_using="btree",
    )

    # (2) The durable step-event log.
    payload_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    # Portable BIGSERIAL: BigInteger + primary_key + autoincrement compiles
    # to BIGSERIAL on PG; Integer on SQLite is the only rowid-aliasing
    # autoincrement shape. Same pattern as event_outbox.event_id (0027).
    seq_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    op.create_table(
        "addon_step_event",
        sa.Column(
            "seq",
            seq_type,
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        # Stable, client-facing event id (distinct from the seq cursor):
        # lets an add-on dedupe on reconnect without depending on the
        # dialect-specific seq value.
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column(
            "pairing_id",
            sa.Uuid(),
            sa.ForeignKey("addon_pairing.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("work_ref", sa.Text(), nullable=True),
        # Convention-only reference to ``audit_log.id`` (no enforced FK,
        # mirroring BroadcastEvent.audit_id): the audit row is the
        # canonical record, this is the downstream durable delivery view.
        sa.Column("audit_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payload",
            payload_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # Resume read: ``WHERE pairing_id = :p AND seq > :after ORDER BY seq``.
    op.create_index(
        "addon_step_event_pairing_seq_idx",
        "addon_step_event",
        ["pairing_id", "seq"],
        postgresql_using="btree",
    )
    # Stable event-id uniqueness (per-tenant would also do; global is
    # simplest and matches the UUID's global namespace).
    op.create_index(
        "addon_step_event_id_idx",
        "addon_step_event",
        ["id"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("addon_step_event_id_idx", table_name="addon_step_event")
    op.drop_index("addon_step_event_pairing_seq_idx", table_name="addon_step_event")
    op.drop_table("addon_step_event")
    op.drop_index(
        "addon_pairing_service_account_sub_idx",
        table_name="addon_pairing",
    )
    op.drop_column("addon_pairing", "service_account_sub")

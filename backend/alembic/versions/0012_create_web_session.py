# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``web_session`` table for BFF session custody.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-22

Initiative #337 (G10.0 Frontend chassis), Task #864 (G10.0-T3). The
operator-console is locked to the Backend-for-Frontend (BFF) custody
shape per locked decision #11 (`docs/planning/v0.2-decisions.md`):
the browser holds nothing but an opaque session-cookie value, the
real OAuth access + refresh tokens live in a server-side row that
the backplane decrypts on every authenticated `/ui/*` request.

This migration ships *only* the storage substrate -- the ORM model
(:class:`meho_backplane.db.models.WebSession`), the
:func:`meho_backplane.ui.auth.session_store.create_session` /
``load_session`` / ``revoke_session`` / ``rotate_refresh`` callers,
and the test suite that asserts the round-trip + replay contracts.
The login / callback / middleware flow that *uses* the substrate is
Task #865 (T4) territory.

Schema
------

* ``id`` -- UUID primary key. The cookie value the browser holds is
  this UUID's canonical 36-char hex form (Python's ``str(uuid)``);
  the BFF middleware (#865) sets ``Set-Cookie: meho_session=<id>;
  HttpOnly; Secure; SameSite=Strict; Path=/`` on session creation
  and parses the same value on every subsequent request. UUID is
  the right shape because (a) it's globally unique without a
  central allocator, (b) PG's ``gen_random_uuid()`` is
  cryptographically random by default (CSPRNG-backed; ~122 bits of
  entropy, enough to make session-id guessing computationally
  infeasible for browser-supplied values), and (c) the existing
  chassis pattern (``audit_log.id``, ``targets.id``,
  ``graph_node.id``) uses UUIDs end-to-end so the dialect-portable
  ``Uuid()`` shape is well-trodden.

* ``operator_sub`` -- Text NOT NULL. The Keycloak ``sub`` claim of
  the operator who logged in. Mirrors :attr:`AuditLog.operator_sub`
  (migration ``0001``, ORM ``db/models.py:343``); the chassis has no
  ``operator`` table, so ``sub`` is the operator's stable identifier
  end-to-end. Indexed by ``web_session_operator_sub_idx`` so the
  "list sessions for operator X" query (a likely future revoke-all
  surface) is a btree probe.

* ``tenant_id`` -- UUID NOT NULL. The operator's active tenant at
  session-creation time, sourced from the
  ``settings.jwt_tenant_claim_name`` claim. No FK to ``tenant(id)``
  -- the same soft-FK discipline ``audit_log.tenant_id`` follows
  (``0002_create_tenant_and_audit_tenant_id``): a session row is
  *write-mostly*, a tenant delete is a major operation that must
  scrub the dependent sessions explicitly before removing the
  tenant row, and adding the FK now would force a backfill cycle
  for any pre-existing chassis-era data when v0.2.next tightens
  the audit FK. Keep the discipline uniform.

* ``access_token`` / ``refresh_token`` -- ``LargeBinary`` NOT NULL.
  ``bytea`` on PostgreSQL, ``BLOB`` on SQLite (dev/test). The
  columns hold the Fernet-encrypted ciphertext of the OAuth bearer
  tokens, never plaintext. Fernet tokens are themselves URL-safe
  base64 strings, but the chassis stores the *bytes* form to avoid
  text-search tooling (psql ``\\d``, future grep-the-audit-export
  flows) ever surfacing what looks like an OAuth token in stable
  storage. The :mod:`meho_backplane.ui.auth.session_store` module
  is the only seam that touches these columns; every read / write
  passes through :class:`cryptography.fernet.Fernet`.

* ``created_at`` / ``expires_at`` -- ``timestamptz`` NOT NULL.
  ``created_at`` defaults to ``now()`` on PG, ORM-side
  ``datetime.now(UTC)`` on SQLite; ``expires_at`` is set by the
  session-creation caller (#865) from the access-token's ``exp``
  claim. ``load_session`` filters on ``expires_at > now()`` so a
  natural-expiry session is invisible without a separate sweeper.

* ``last_seen_at`` -- ``timestamptz`` NOT NULL. Refreshed on every
  successful :func:`load_session` call (server-side write, no
  client-controlled value); future idle-revocation can run a tick
  off this column. Initialised to ``now()`` at create time -- the
  semantics match "last time the operator presented this session
  cookie", which at row-creation is the create call itself.

* ``revoked_at`` -- ``timestamptz`` NULL. NULL means the session is
  still active (subject to the expiry check); non-NULL means it has
  been explicitly revoked (logout, refresh-token replay detection,
  operator-side global-revoke). Soft-delete shape preserves the
  audit trail: a revoked-session row stays queryable for
  forensics; the read-side filter is ``revoked_at IS NULL AND
  expires_at > now()``.

  Why soft-delete instead of ``DELETE``: the audit row written on
  refresh-token replay references the revoked session by
  :attr:`AuditLog.path` (``ui.session.refresh_replay``) and a free-
  form payload that includes the session id. Hard-deleting the row
  here would leave the audit row dangling (we cannot back-link via
  FK because the soft-FK discipline applies); keeping the row
  visible-but-marked-revoked preserves the audit chain at the cost
  of one column. The chassis precedent is :class:`Tenant` (deleted
  via a future "tenant lifecycle" Initiative, not by this layer)
  and :class:`GraphNode` (refresh-driven soft-deletes set
  ``last_seen=NULL``); neither hard-deletes from a column-bearing
  identifier.

Indexes
-------

* ``web_session_operator_sub_idx`` -- btree on ``operator_sub``.
  Drives the future "list / revoke all sessions for operator X"
  surface (no current consumer; the index is cheap to maintain --
  one column, append-mostly writes -- and worth the ~ms-on-write
  cost so the read query is not a sequential scan when it lands).

* ``web_session_expires_at_idx`` -- btree on ``expires_at``. Drives
  the future background sweep "delete expired sessions older than
  N days" (out of v0.2 scope; the index is the cheap part of the
  forward-compat). The read-side ``load_session`` query filters on
  ``id = ?`` and re-checks ``expires_at`` against ``now()`` -- the
  PK index already does the heavy lifting for that path; this
  index is for the sweep, not the hot path.

Dialect portability
-------------------

* ``id`` server default -- ``gen_random_uuid()`` on PG (built-in
  since PG 13, the chassis floor); SQLite leaves it to the ORM
  ``default=uuid.uuid4`` (set on the model).
* ``created_at`` / ``last_seen_at`` server default -- ``now()`` on
  PG; SQLite leaves it to the ORM ``default=lambda:
  datetime.now(UTC)``.
* ``LargeBinary`` -- ``bytea`` on PG, ``BLOB`` on SQLite. Identical
  Python contract (``bytes``) on both dialects.

Reversibility contract
----------------------

``upgrade()`` creates the table and its two indexes; ``downgrade()``
drops the indexes then the table in inverse order. SQLite does not
always auto-cascade index drops on ``DROP TABLE``; explicit drops
keep the inverse symmetric across both dialects, matching the
discipline migrations ``0007`` and ``0008`` established.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``web_session`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "web_session",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Keycloak ``sub`` claim. Soft-FK discipline -- no FK to an
        # ``operator`` table (which does not exist in v0.2). Mirrors
        # ``audit_log.operator_sub`` (migration ``0001``).
        sa.Column("operator_sub", sa.Text(), nullable=False),
        # Soft-FK to ``tenant.id`` -- same discipline as
        # ``audit_log.tenant_id`` (see ``0002`` docstring).
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Fernet-encrypted OAuth tokens. ``bytea`` on PG, ``BLOB`` on
        # SQLite. The plaintext never lands in this column -- every
        # write passes through ``meho_backplane.ui.auth.session_store``
        # which Fernet-encrypts via the chassis-wide key resolved from
        # ``settings.ui_session_encryption_key``.
        sa.Column("access_token", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # NULL = active; non-NULL = revoked (logout, refresh replay,
        # operator-side global-revoke). Soft-delete shape so the audit
        # trail (audit_log rows that reference this session id) stays
        # back-traceable.
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "web_session_operator_sub_idx",
        "web_session",
        ["operator_sub"],
        postgresql_using="btree",
    )
    op.create_index(
        "web_session_expires_at_idx",
        "web_session",
        ["expires_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop indexes then the ``web_session`` table."""
    op.drop_index("web_session_expires_at_idx", table_name="web_session")
    op.drop_index("web_session_operator_sub_idx", table_name="web_session")
    op.drop_table("web_session")

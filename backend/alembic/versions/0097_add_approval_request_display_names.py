# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request`` display-name columns -- who, not just a GUID.

Revision ID: 0097
Revises: 0096
Create Date: 2026-09-05

Task #3300 under Initiative #3301 (console + CLI GUID-to-name resolution).
Every operator-facing approvals surface -- CLI ``approve`` / ``show`` /
``list``, the ``/decide`` response, the console approvals modal / history /
panel -- renders the requester (``principal_sub``) and approver
(``reviewed_by``) as bare OIDC ``sub`` GUIDs. An operator reviewing or
auditing an approval sees an identity GUID at the exact moment they most
need to know *who*.

There is no ``sub`` -> name join table (the agent / runner principal tables
key on the Keycloak client id, not the token ``sub`` -- see
``meho_backplane.ui.references``), so the name cannot be resolved after the
fact. This migration ships the storage half of the same hoist-at-write
pattern ``audit_log.principal_name`` already uses (#1212): the display name
is captured from the acting operator's JWT ``name`` claim at the moment the
credential acts, and cached on the row alongside the stable ``sub``.

What this migration adds
------------------------

* ``approval_request.principal_name text NULL`` -- the requester's display
  name, captured at park time from ``Operator.name``.
* ``approval_request.reviewed_by_name text NULL`` -- the reviewer's display
  name, captured at decision (approve / reject) time from ``Operator.name``.

Both carry **display name only** -- no email, groups, or other profile
fields (the approvals contract keeps PII off these surfaces; ``#1212``
already carries email on audit rows where it exists and that is not widened
here). The name is always shown *alongside* the ``sub``, never instead: the
``sub`` stays the stable, machine-truthful key. Resolution fails open --
a NULL name (token carried no ``name``, or a pre-0097 row) degrades cleanly
to the GUID the surface shows today, and populating an in-memory column
value can never fail or slow an approval decision.

Soft-column discipline mirrors ``0036`` / ``0040`` / ``0053`` / ``0055`` /
``0086`` / ``0096``: nullable, no server default (Python-side ``None`` /
capture-set), reversible, no indexes (both columns are only ever read off a
row already loaded by primary key or a tenant-scoped list).

Migration-chain note (single linear head)
-----------------------------------------

Numbered ``0097`` with ``down_revision = "0096"`` -- the head on
``origin/main`` (the ``0089`` / ``0090`` / ``0093`` gaps are renumbered
siblings from earlier chains). Additive-only: two nullable columns on an
existing table, no ALTER of an existing column.

Reversibility contract
----------------------

``downgrade()`` drops both columns. SQLite's ALTER TABLE drop-column has
been supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode fallback
is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable display-name columns to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column("principal_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "approval_request",
        sa.Column("reviewed_by_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the display-name columns added in :func:`upgrade`."""
    op.drop_column("approval_request", "reviewed_by_name")
    op.drop_column("approval_request", "principal_name")

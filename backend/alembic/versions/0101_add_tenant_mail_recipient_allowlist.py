# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant mail-recipient allowlist (#3499).

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-08

Task #3499 (Envision isolation, meho-internal#320). Ships the per-tenant
narrowing override for the ``mail.*`` connector's recipient floor. Today
``MAIL_RECIPIENT_ALLOWLIST`` is a single instance-wide env, so on a shared
instance an approved ``mail.send`` in one tenant can deliver to the recipients
another tenant configured. This column lets an operator pin a tenant to no mail
(or a narrower recipient set) while other tenants keep their alert mail.

What this migration adds
------------------------

``tenant.mail_recipient_allowlist`` -- a nullable ``Text`` column carrying a
tri-state on the *value*, not a Boolean:

* ``NULL`` (default) -- **inherit**: no per-tenant narrowing; the deployment
  ``MAIL_RECIPIENT_ALLOWLIST`` instance floor alone governs this tenant's
  dispatched ``mail.send``.
* ``""`` (empty string) -- **deny**: the tenant's parsed allowlist is empty, so
  every dispatched ``mail.send`` for this tenant is refused (the inverted
  "empty ⇒ inert" default the instance floor already uses).
* a comma-separated address/domain string -- the recipients this tenant may
  mail, still intersected with the instance floor at the transport (a tenant
  can only narrow, never widen past the floor).

Read per dispatch by the cache-aware resolver in
``meho_backplane.connectors.mail.tenant_policy`` and mutated through
``PATCH /api/v1/tenants/mail-recipient-policy`` (tenant_admin). The checks
notifier's direct-import ``send_email`` path is not tenant-dispatched and keeps
only the instance floor.

Additive-only, per the migration-compatibility house rule -- one ``ADD COLUMN``
on ``tenant``, no ALTER of an existing column and no data rewrite. The column is
**nullable** (NULL = inherit), so the add-column needs no backfill and is safe
on a populated table.

Reversibility contract
----------------------

``downgrade()`` drops the single added column. SQLite ALTER-TABLE drop-column
is supported since 3.35.0 (we run 3.45+), so Alembic batch-mode is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0101"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-tenant mail-recipient allowlist (#3499). Nullable Text, so no
    # backfill -- existing tenants keep NULL (= inherit the instance floor),
    # preserving today's behaviour with no value change.
    op.add_column(
        "tenant",
        sa.Column("mail_recipient_allowlist", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant", "mail_recipient_allowlist")

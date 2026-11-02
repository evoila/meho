# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide the secret-named values in the params of every decided approval request.

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-02

``approval_request.params`` keeps the params of a waiting request so that
MEHO can run the approved call with them. A deployment spec in those params
holds passwords and licence keys. Since this release the queue replaces
each secret-named value with a marker as soon as the run claim is won or
the request ends without a run (``approval_queue.claim_resume`` /
``end_run_input``). This one-off step does the same for the rows that were
decided before: every row whose ``status`` is not ``pending`` and whose
``params`` hold a secret-named value. A pending row is left alone: its
run still needs the values, and the queue hides them when it ends.

The name rule is the one the queue uses
(:func:`meho_backplane.broadcast.events.scrub_secret_named_values`:
``password``, ``licenseKey``, ``client_secret``, ...; a number under such
a name is hidden too). It is imported inside ``upgrade()`` on purpose: the rule is a pure function, not an ORM
model, and one copy of it keeps this step and the running code in
agreement. The other fields of the params keep their value.

Read-mutate-write per row, as in ``0054``: neither SQLite nor PostgreSQL
rewrites a JSON element in a portable way. Only a row whose params change
is written, so a re-run is a no-op.

``downgrade()`` is a documented no-op: the replaced values were secrets and
must not come back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0132"
down_revision: str | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PENDING = "pending"


def upgrade() -> None:
    """Replace each secret-named value in the params of every decided row."""
    from meho_backplane.broadcast.events import scrub_secret_named_values

    requests = sa.table(
        "approval_request",
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.Text()),
        sa.column("params", sa.JSON()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(requests.c.id, requests.c.params).where(
            requests.c.status != _PENDING, requests.c.params.is_not(None)
        )
    ).fetchall()
    for row_id, params in rows:
        hidden, found = scrub_secret_named_values(params, hide_numbers=True)
        if not found:
            continue
        bind.execute(sa.update(requests).where(requests.c.id == row_id).values(params=hidden))


def downgrade() -> None:
    """No-op by design: the replaced values were secrets and must not come back."""
    # Intentionally empty -- see docstring.

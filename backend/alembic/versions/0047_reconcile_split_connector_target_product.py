# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile existing ``_PRODUCT_SPLITS`` ``targets.product`` long→short.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-17

Initiative #1810 (retire the long↔short connector product divergence),
Task #1814 (the ATOMIC realign + migration). The data half of the
family-wide product-identity realignment — the direct analogue of
migration ``0046`` for vRLI (#1798).

Why this migration exists
-------------------------

#1814 realigned the five sanctioned ``_PRODUCT_SPLITS`` connectors to
register under their short, dispatch-canonical product token, keeping
each ``impl_id``/``version`` unchanged:

================  ====================  =================
connector class   old registry product  new (short) token
================  ====================  =================
SddcManager       ``sddc-manager``      ``sddc``
VcfAutomation     ``vcf-automation``    ``vcfa``
VcfFleet          ``vcf-fleet``         ``fleet``
VcfOperations     ``vcf-operations``    ``vrops``
HetznerRobot      ``hetzner-robot``     ``hetzner``
================  ====================  =================

The dispatch/resolve path matches a stored ``targets.product`` against
``Connector.product`` **verbatim**
(:func:`~meho_backplane.connectors.resolver._filter_candidates` — no
canonicalisation on the read path; the long↔short bridges
``dispatch_product`` / ``PRODUCT_ALIASES`` act only on the ingest-write
and target-write surfaces, never on an already-stored row at dispatch).
So after the realignment a stored target carrying one of the five long
tokens — the exact spelling these connectors registered under **before**
#1814, hence the spelling operators' existing targets carry — would
resolve ``NoMatchingConnector`` on its next dispatch / topology refresh.
This migration rewrites those rows to the short token so they keep
dispatching, exactly as ``0046`` did for ``product="vcf-logs"`` vRLI
targets.

This is the load-bearing safety piece of #1814: realigning the
connectors without it ships a breaking operator-facing regression with
no remediation. #1798 deliberately bundled vRLI's realign with migration
``0046`` for the same reason.

Fix shape — Alembic data migration
-----------------------------------

Same self-contained shape as migration ``0046`` (and ``0011`` / ``0038``
before it): a lightweight :func:`sa.table` / :func:`sa.column` shim
mirrors only the columns the migration touches, no ORM imports
(importing the live models would pin the migration to one moment in the
schema's history and break replay), and each statement executes
synchronously via ``op.get_bind()``.

Per-product scoping
-------------------

The rewrite is applied one mapping at a time, each ``UPDATE`` narrowed to
its single ``from_product`` value, so a row under any *other* product
(``vmware``, ``k8s``, ``vrli``, an operator's custom product, …) is never
touched. The five short target tokens are pairwise distinct and distinct
from every other shipped product, so the mappings cannot interfere with
one another.

Row-narrowing predicate
-----------------------

A row is rewritten only when **both** hold (identical to ``0046``):

* ``product = <long token>`` — the row still carries the pre-#1814
  spelling. This is also what makes each ``UPDATE`` idempotent: after the
  rewrite the row carries the short token and no longer matches, so a
  re-run (or the stamp-back replay the test suite exercises) is a no-op.
* ``deleted_at IS NULL`` — soft-deleted targets (migration ``0029``) are
  tombstones; leaving them under the stale spelling preserves the audit
  trail of what the operator originally created and never resurrects a
  deleted row into the live, dispatchable set.

Tenant-scoped rows are rewritten: the ``targets`` table is per-tenant by
construction (``tenant_id`` NOT NULL, migration ``0004``), so the
operators' own targets are exactly the rows that must move. There is no
built-in / global target namespace to preserve.

Collision safety
----------------

The only unique constraint on ``targets`` is ``targets_tenant_name_idx``
on ``(tenant_id, name)`` (migration ``0004``) — **not** on ``product``.
Rewriting ``product`` leaves ``(tenant_id, name)`` untouched, so the
``UPDATE`` cannot collide with that index.

Reversibility contract
----------------------

``downgrade()`` rewrites each short token back to its long spelling. Like
``0046`` (and unlike ``0038``'s no-op downgrade), the long spellings
*were* dispatchable on the pre-#1814 image (where the connectors
registered under them), so restoring them on rollback keeps those targets
resolvable against the older code. The downgrade is scoped by the same
predicate (``product = <short token>``, ``deleted_at IS NULL``); it is a
best-effort inverse for an image revert, accepting that a genuinely
short-spelled target an operator created *after* this migration is
indistinguishable from a migrated one and would also be rewritten — an
acceptable rollback approximation, since on the older image the long form
resolved the connector anyway.

Cross-references
----------------

* Task #1814 / Initiative #1810 — this reconciliation.
* Migration ``0046`` — the vRLI sibling this mirrors (#1798).
* :func:`~meho_backplane.connectors.resolver._filter_candidates` — the
  verbatim ``target.product`` match that makes this migration load-bearing.
* :mod:`tests.test_migration_0047_reconcile_split_connector_target_product`
  — the behavioural contract: rewrite, per-product scoping, soft-delete
  boundary, idempotency, and the downgrade inverse.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The five product renames #1814 performs, keyed long → short. vRLI was
#: already reconciled by ``0046`` and is deliberately absent.
_PRODUCT_RENAMES: Mapping[str, str] = {
    "sddc-manager": "sddc",
    "vcf-automation": "vcfa",
    "vcf-fleet": "fleet",
    "vcf-operations": "vrops",
    "hetzner-robot": "hetzner",
}


def _targets_shim() -> sa.Table:
    """Return a minimal :func:`sa.table` shim for the columns we touch."""
    return sa.table(
        "targets",
        sa.column("product", sa.Text()),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _rewrite_product(*, from_product: str, to_product: str, now: datetime) -> None:
    """Move live (non-soft-deleted) targets from one product token to another."""
    targets = _targets_shim()
    stmt = (
        sa.update(targets)
        .where(
            targets.c.product == from_product,
            targets.c.deleted_at.is_(None),
        )
        .values(product=to_product, updated_at=now)
    )
    op.get_bind().execute(stmt)


def upgrade() -> None:
    """Rewrite live ``targets.product`` from each long token to its short form.

    ``updated_at`` is bumped (single ``now`` per run, same discipline as
    migrations ``0011`` / ``0038`` / ``0046``) so operator tooling driven
    off the column sees the change. Each mapping is a separately-scoped
    ``UPDATE`` so unrelated products are never touched.
    """
    now = datetime.now(UTC)
    for long_product, short_product in _PRODUCT_RENAMES.items():
        _rewrite_product(from_product=long_product, to_product=short_product, now=now)


def downgrade() -> None:
    """Restore live ``targets.product`` from each short token to its long form.

    Best-effort inverse for an image revert: each long spelling was
    dispatchable on the pre-#1814 image, so restoring it keeps those
    targets resolvable against the older code. See the module docstring's
    "Reversibility contract" for the rollback approximation this accepts.
    """
    now = datetime.now(UTC)
    for long_product, short_product in _PRODUCT_RENAMES.items():
        _rewrite_product(from_product=short_product, to_product=long_product, now=now)

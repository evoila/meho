# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile existing vRLI ``targets.product`` from ``vcf-logs`` to ``vrli``.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-16

Initiative #1800 (G0.26 v0.16.0 closed-loop dogfood hardening), Task
#1798 (T4). The data half of the vRLI product-identity realignment.

Why this migration exists
-------------------------

Before #1798, :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
registered under ``product="vcf-logs"`` while the spec-ingest derived
``product="vrli"`` from the ``vrli-rest`` impl_id and auto-registered a
``GenericRestConnector`` shim under ``(vrli, …)``. That shim made
``"vrli"`` a *registered* token, so an operator target with
``product="vrli"`` validated at ``POST /api/v1/targets`` — but dispatch
then resolved the **shim** (``auth_headers`` → ``NotImplementedError``),
never ``VcfLogsConnector`` under the ``vcf-logs`` namespace. That was the
v0.16.0 SEV-2.

#1798 aligns the connector's registry identity to the dispatch-canonical
``product="vrli"``. After the realignment the two operator-facing target
spellings need different treatment:

* ``product="vrli"`` targets — **no data change needed**. The token now
  resolves directly to ``VcfLogsConnector`` (the hand-rolled class
  outranks any auto-shim via the resolver's
  ``hand_rolled_over_shim`` tie-break), so the previously-broken rows
  are fixed by the code realignment alone.
* ``product="vcf-logs"`` targets — **rewritten here to ``"vrli"``**.
  ``"vcf-logs"`` is no longer a registered product token after the
  realignment (the registry now advertises ``"vrli"``), so these rows
  would become unresolvable (``no_connector``) and would 422 on a
  ``PATCH`` round-trip. This migration moves them to the canonical
  token so they keep dispatching. Such rows exist where an operator
  followed the historical connector-listing / docs spelling, or
  mirrored the pre-#1798 e2e fixture, that used ``product="vcf-logs"``.

No new ``PRODUCT_ALIASES`` entry is added — the issue forbids the
per-spelling band-aid; this is a one-time data reconciliation, and the
runtime identity is now single-sourced on the connector registration.

Fix shape — Alembic data migration
-----------------------------------

Same self-contained shape as migrations ``0011`` (the ``when_to_use``
backfill) and ``0038`` (the endpoint-descriptor product-split backfill):
a lightweight :func:`sa.table` / :func:`sa.column` shim mirrors only the
columns the migration touches, no ORM imports (importing the live models
would pin the migration to one moment in the schema's history and break
replay), and the statement executes synchronously via ``op.get_bind()``.

Row-narrowing predicate
-----------------------

A row is rewritten only when **both** hold:

* ``product = 'vcf-logs'`` — the row still carries the pre-#1798
  spelling. This is also what makes the UPDATE idempotent: after the
  rewrite the row carries ``"vrli"`` and no longer matches, so a re-run
  (or the stamp-back replay the test suite exercises) is a no-op.
* ``deleted_at IS NULL`` — soft-deleted targets (migration ``0029``) are
  tombstones; leaving them under the stale spelling keeps the audit
  trail of what the operator originally created and never resurrects a
  deleted row into the live, dispatchable set.

Unlike migration ``0038``, **tenant-scoped rows are rewritten**: the
``targets`` table is per-tenant by construction (``tenant_id`` is NOT
NULL — migration ``0004``), so the operator's own vRLI targets are
exactly the rows that must move. There is no built-in / global target
namespace to preserve.

Collision safety
----------------

The only unique constraint on ``targets`` is ``targets_tenant_name_idx``
on ``(tenant_id, name)`` (migration ``0004``) — **not** on ``product``.
Rewriting ``product`` leaves ``(tenant_id, name)`` untouched, so the
UPDATE cannot collide with that index. A tenant cannot already hold both
a ``vcf-logs`` and a ``vrli`` target sharing one name (the name index
would have rejected the second create), so no twin-existence guard is
required.

Reversibility contract
----------------------

``downgrade()`` rewrites ``"vrli"`` targets back to ``"vcf-logs"``.
Unlike migration ``0038``'s no-op downgrade (whose long spellings were
never dispatchable), the ``"vcf-logs"`` spelling *was* dispatchable on a
pre-#1798 image (where the connector registered under it), so restoring
it on rollback keeps those targets resolvable against the older code.
The downgrade is scoped by the same predicate (``product = 'vrli'``,
``deleted_at IS NULL``); it is a best-effort inverse for an image
revert, accepting that a genuinely ``vrli``-spelled target the operator
created *after* this migration (and meant as ``vrli``) is
indistinguishable from a migrated one and would also be rewritten — an
acceptable rollback approximation, since on the older image both forms
resolved the vRLI connector anyway.

Cross-references
----------------

* Task #1798 / Initiative #1800 (G0.26) — this reconciliation.
* :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
  — the connector whose ``product`` aligned to ``"vrli"``.
* Migration ``0038`` — the sibling backfill for the
  ``endpoint_descriptor`` / ``operation_group`` rows (the family-wide
  split; vRLI's rows already reconcile to ``"vrli"`` via
  ``_reconciled_row_product``).
* Migration ``0011`` — the self-contained data-backfill precedent.
* Initiative #1810 — the deferred family realignment that will migrate
  the remaining five ``_PRODUCT_SPLITS`` connectors' targets.
* :mod:`tests.test_migration_0046_reconcile_vrli_target_product` — the
  behavioural contract: rewrite, soft-delete boundary, idempotency, and
  the downgrade inverse.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The one product rename this migration performs. vRLI is the only
#: connector #1798 realigns; the remaining ``_PRODUCT_SPLITS`` family is
#: deferred to Initiative #1810 (which will extend this pattern).
_LONG_PRODUCT = "vcf-logs"
_CANONICAL_PRODUCT = "vrli"


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
    """Rewrite live ``product='vcf-logs'`` vRLI targets to ``'vrli'``.

    ``updated_at`` is bumped (single ``now`` per run, same discipline as
    migrations ``0011`` / ``0038``) so operator tooling driven off the
    column sees the change.
    """
    _rewrite_product(
        from_product=_LONG_PRODUCT,
        to_product=_CANONICAL_PRODUCT,
        now=datetime.now(UTC),
    )


def downgrade() -> None:
    """Restore live ``product='vrli'`` vRLI targets to ``'vcf-logs'``.

    Best-effort inverse for an image revert: the ``"vcf-logs"`` spelling
    was dispatchable on the pre-#1798 image, so restoring it keeps those
    targets resolvable against the older code. See the module docstring's
    "Reversibility contract" for the rollback approximation this accepts.
    """
    _rewrite_product(
        from_product=_CANONICAL_PRODUCT,
        to_product=_LONG_PRODUCT,
        now=datetime.now(UTC),
    )

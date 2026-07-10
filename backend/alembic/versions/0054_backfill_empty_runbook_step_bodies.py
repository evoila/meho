# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backfill empty / whitespace-only runbook template step bodies.

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-10

Initiative #2286 (G0.30 v0.20.0 closed-loop dogfood hardening), Task #2239.
The data half of a fix PR #2122 (Initiative #2117, ancestor of v0.20.0)
shipped forward-only.

Why this migration exists
-------------------------

PR #2122 tightened both runbook step-body fields with
``StringConstraints(strip_whitespace=True, min_length=1)``
(:class:`~meho_backplane.runbooks.schemas.OperationCallStep` and
:class:`~meho_backplane.runbooks.schemas.ManualStep`, ``schemas.py``) but
shipped **no data migration**. The service layer re-validates the stored
``runbook_templates.steps`` JSONB back through
:class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody` on every
read -- a *deliberate, documented* fail-closed posture
(:func:`~meho_backplane.runbooks.service._steps_from_storage`, docstring
at ``service.py``). So any pre-v0.20.0 row whose step body is empty or
whitespace-only now raises :class:`pydantic.ValidationError`
(``string_too_short`` at ``('steps', N, <variant>, 'body')``) at read
time: REST ``GET /api/v1/runbooks/templates/{slug}`` 500s, MCP
``meho.runbook.show_template`` returns ``-32603``, and -- the wider blast
radius -- ``list_runs`` breaks **tenant-wide** because it hydrates every
run's pinned template with no per-row guard (``run_service.py``: the
``_load_pinned_template_or_none`` sink). A point release made
pre-existing customer data unreadable with no visible diagnostic.

Fix shape -- remove the offending data, keep the fail-closed read
--------------------------------------------------------------------

The rejected alternative is relaxing the read-side re-validation
(validate-on-write-only): the fail-closed posture is intentional -- a row
that reached storage malformed should surface, not leak an unvalidated
shape to the caller. So this migration removes the *data* the constraint
now rejects instead: every step body matching the constraint's own
invalidity condition (``body.strip() == ""`` -- exactly what
``strip_whitespace=True, min_length=1`` rejects) is rewritten to a
non-empty placeholder. One rewrite of the ``runbook_templates`` table
repairs **all** read sinks at once, run-side included: runs pin
``(slug, version)`` and re-read the same ``runbook_templates`` row, so a
repaired template hydrates cleanly through ``show_template`` *and*
``list_runs``.

Both step variants carry the tightened ``body`` field, so the rewrite is
variant-agnostic: it keys on the ``body`` value of every step dict, not
on ``type``. The complementary structured-error envelopes on the
template-show REST + MCP surfaces (same task) make any *future* malformed
row diagnosable instead of an opaque 500; this migration clears the
existing ones.

Tenant-agnostic
---------------

Unlike the ``0038`` / ``0049`` / ``0052`` product-reconciliation
migrations, this one does **not** scope to ``tenant_id IS NULL``: the
poisoned rows are operator-authored templates (the built-in set ships no
empty bodies), so every tenant's rows are in scope. There is no natural
key to collide with -- only the ``steps`` JSONB *content* is rewritten,
never any part of the ``(tenant_id, slug, version)`` unique key -- so the
``0038`` collision guard (migrations.md rule 7) does not apply.

``edited_at`` / ``edited_by`` are deliberately **not** touched: this is a
system data-repair, not an operator edit. Bumping ``edited_at`` without a
matching ``edited_by`` change would misattribute the rewrite and churn
the ``list_templates`` ``edited_at``-desc ordering; the audit trail of
who last authored the template stays honest.

Idempotency
-----------

Self-idempotent by construction: the rewrite replaces an empty body with
a non-empty placeholder, so on a re-run (or a stamp-back replay) no step
body matches ``body.strip() == ""`` any more and the row-level ``changed``
guard skips the UPDATE entirely -- no statement is issued, nothing moves.
No ``updated_at`` to worry about (none is bumped).

Reversibility contract
----------------------

``downgrade()`` is a documented no-op, same rationale as ``0011`` /
``0038`` / ``0052``: the pre-upgrade value (an empty string) was invalid
under the current schema and carries no operator-recoverable state --
restoring it would need a copy column and would only re-break the read
path. No DDL runs in ``upgrade()``, so there is nothing to undo at the
schema layer; production rollback is image-revert under the additive-only
forward-compat contract (``docs/codebase/migrations.md``).

Fix shape -- Alembic data migration
-----------------------------------

Same self-contained shape as ``0011`` / ``0038`` / ``0052``: a
lightweight :func:`sa.table` / :func:`sa.column` shim mirrors only the
columns touched (``id`` + ``steps``), no ORM imports (replay safety). The
``steps`` JSONB is read into Python, rewritten in memory, and written
back per row -- a portable read-mutate-write, since neither SQLite nor
PostgreSQL exposes a portable in-place JSON element rewrite. The typed
``sa.Uuid()`` / ``sa.JSON()`` shim columns carry SQLAlchemy's dialect
bind/result processors, so the ``id`` round-trips correctly on both
dialects (``value.hex`` on SQLite, native ``uuid`` on PG) with no manual
bind coercion, and no ``str(uuid)`` bind is written (the
``migrations.md`` UUID rule / drift-guard).

Cross-references
----------------

* Task #2239 / Initiative #2286 -- this fix.
* PR #2122 / Initiative #2117 -- the forward-only ``min_length=1``
  tightening whose data half this completes.
* :func:`~meho_backplane.runbooks.service._steps_from_storage` -- the
  read-side re-validation sink the poisoned rows fail.
* :mod:`meho_backplane.runbooks.hydration_errors` -- the structured-error
  envelope for the residual / future malformed row (same task).
* Migrations ``0011`` / ``0038`` / ``0052`` -- the backfill-rewrite mold
  (self-contained shim, documented no-op downgrade).
* :mod:`tests.migrations.test_migration_0054_backfill_empty_runbook_step_bodies`
  -- the behavioural contract, incl. the whitespace-only case and the
  ``show_template`` + ``list_runs`` post-migration hydration probe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The placeholder written over an empty / whitespace-only step body. Must
#: be non-empty and non-whitespace (so it satisfies the ``min_length=1`` +
#: ``strip_whitespace=True`` constraint the read path re-checks) and carry
#: no ``${...}`` substitution token (so it passes the publish/read-time
#: substitution allowlist). The prose names *why* the body is empty so an
#: operator reading the repaired template understands it was auto-filled.
_PLACEHOLDER_BODY: str = (
    "(no instructions recorded — authored before the v0.20.0 non-empty-body requirement)"
)


def _backfill_step_bodies(steps: object) -> tuple[object, bool]:
    """Return *steps* with empty / whitespace-only step bodies replaced.

    Walks the stored ``steps`` list and rewrites every step dict whose
    ``body`` is a string that strips to empty -- the exact condition
    ``StringConstraints(strip_whitespace=True, min_length=1)`` rejects, so
    the set rewritten is precisely the set the read path fails on. Both
    step variants (``operation_call`` / ``manual``) carry ``body``, so the
    check is variant-agnostic (keys on the value, not ``type``).

    Returns ``(new_steps, changed)``; ``changed`` is ``False`` when nothing
    matched (a fresh DB, a re-run, an already-repaired row), so the caller
    can skip the UPDATE and keep the migration idempotent. A non-list
    ``steps`` or a non-string ``body`` is left untouched -- this migration
    repairs the empty-body case only; any other malformed shape surfaces
    via the read-path structured envelope, not here.
    """
    if not isinstance(steps, list):
        return steps, False
    changed = False
    new_steps: list[object] = []
    for step in steps:
        if isinstance(step, dict):
            body = step.get("body")
            if isinstance(body, str) and body.strip() == "":
                step = {**step, "body": _PLACEHOLDER_BODY}
                changed = True
        new_steps.append(step)
    return new_steps, changed


def upgrade() -> None:
    """Rewrite every empty / whitespace-only step body to the placeholder.

    Read-mutate-write per row: only rows that actually carry an empty body
    are UPDATE-d (the ``changed`` guard), so a fresh or already-repaired
    database issues no writes.
    """
    templates = sa.table(
        "runbook_templates",
        sa.column("id", sa.Uuid()),
        sa.column("steps", sa.JSON()),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(templates.c.id, templates.c.steps)).fetchall()
    for row_id, steps in rows:
        new_steps, changed = _backfill_step_bodies(steps)
        if not changed:
            continue
        bind.execute(sa.update(templates).where(templates.c.id == row_id).values(steps=new_steps))


def downgrade() -> None:
    """No-op by design.

    The rewritten value was an empty string that the current schema
    rejects and that carried no operator-recoverable state; restoring it
    would need a copy column and would only re-break the read path. No DDL
    runs in ``upgrade()``, so there is nothing to undo at the schema layer.
    Same documented-no-op shape as ``0011`` / ``0038`` / ``0052``. The
    function stays defined so ``alembic downgrade -1`` resolves the symbol
    cleanly.
    """
    # Intentionally empty -- see docstring.

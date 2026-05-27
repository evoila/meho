# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.raw_payload`` + ``audit_log.redaction_manifest`` columns.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-26

Schema half of Task #1071 (G11.4-T2) under Initiative #805. Wires the
connector-boundary redaction middleware into the audit trail: the
dispatcher captures the raw connector response, hands it to the
:mod:`meho_backplane.redaction` engine, stores the raw payload verbatim
on the audit row, and persists the engine's manifest alongside. The
**caller / LLM only ever sees the redacted form**; the raw record is
internal-only and gated by the audit-query RBAC the chassis already
enforces on the rest of the row.

What this migration adds
------------------------

* ``audit_log.raw_payload`` -- ``JSONB`` on PostgreSQL, generic ``JSON``
  on SQLite. The verbatim connector response, normalised to JSON-shaped
  containers by
  :func:`~meho_backplane.redaction.middleware.normalize_for_audit`
  (Pydantic models flatten to dicts; tuples / sets flatten to lists;
  bytes become hex). Nullable: error-path rows (handler raised, no
  response produced) and pre-G11.4 rows carry NULL.

* ``audit_log.redaction_manifest`` -- ``JSONB`` on PostgreSQL, generic
  ``JSON`` on SQLite. The structured record of every rule firing the
  engine emitted while reducing ``raw_payload`` to the redacted view
  the caller saw. Each entry carries the rule name, the named pattern,
  the action (``redact`` / ``mask`` / ``hash``), the match count, the
  per-rule input span of the first match, the rule's ``reason`` string,
  and the dotted JSON path to the leaf. Plus the resolved
  ``policy_id`` (top-level key under the manifest dict), used by the
  C1-d round-trip CI gate (#1073) to replay the same policy against
  ``raw_payload`` and assert byte-identical redacted output across
  policy revisions. Nullable for the same reasons as ``raw_payload``.

No backfill -- pre-G11.4 audit rows stay NULL on both columns. Audit
queries that need to display "raw / redacted / manifest" simply skip
rows where the columns are NULL (or render the existing ``payload``
column as the only available view; the redaction middleware is the
single writer of these columns).

Why no index
------------

The audit-query surface (G8.1) already indexes
``(occurred_at, operator_sub, tenant_id, target_id, parent_audit_id,
agent_session_id, actor_sub)`` -- the seven dimensions an operator
filters by. The new columns are **payload-shape data**, not query
predicates: an operator queries for "what did agent X do" and then
inspects the raw/manifest pair on the matching rows. Indexing JSON
content of the manifest is a meaningful query-time question only once
audit-replay surfaces (G8.2-next) need it; until then, an unused
index is pure write overhead. The PostgreSQL ``JSONB`` type itself is
GIN-indexable later without a column rewrite when that need lands.

Why additive (not amend ``0021``)
---------------------------------

Migration ``0021`` (``audit_log.actor_sub``) is merged to ``main`` and
applied in CI and dev databases. The reversible-additive discipline
established by ``0006``+ applies: new requirement = new migration
head, never rewrite historical migrations. Same rationale as ``0014``
(``agent_session_id``), ``0021`` (``actor_sub``), ``0029``
(``targets.deleted_at``).

Dialect-portability decisions
-----------------------------

* Both columns use generic ``sa.JSON`` rather than dialect-specific
  ``JSONB``. The model definition pins ``JSONB`` on PostgreSQL via
  ``with_variant`` (see ``_PORTABLE_JSON`` in ``db/models.py``); the
  migration runs against SQLite in the test suite, where ``JSON``
  compiles to a generic text column with the same ``json.dumps`` /
  ``json.loads`` round-trip semantics. This is the same pattern as
  the existing ``payload`` column on ``audit_log``.
* ``nullable=True`` on both columns. Adds a column to a populated
  table without a server default; existing rows carry NULL. Required
  for `helm rollback` compatibility (Goal #11 DoD §3) -- a rollback
  to v0.2.previous against a v0.2.next-migrated DB sees the NULLs
  in fields the older code doesn't read.

Reversibility contract
----------------------

``downgrade()`` drops both columns in reverse order. SQLite ALTER
TABLE DROP COLUMN has been supported since 3.35.0 (we're on 3.45+);
Alembic's batch-mode fallback is not required. PostgreSQL drops the
column directly. No data backfill is performed so the downgrade is
purely destructive of post-G11.4 data on those rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``raw_payload`` + ``redaction_manifest`` columns to ``audit_log``."""
    op.add_column(
        "audit_log",
        sa.Column("raw_payload", sa.JSON(), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("redaction_manifest", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the two columns in reverse order."""
    op.drop_column("audit_log", "redaction_manifest")
    op.drop_column("audit_log", "raw_payload")

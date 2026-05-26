# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Supersede the ``rdc-internal`` seed with a generic ``default`` tenant.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-26

Initiative #1130 (G0.13 v0.6.0 dogfood hardening), Task #1137 (T7).
This is a **data migration** -- no schema changes -- that closes the
OSS commercialization-readiness gap migration ``0018`` opened: the
public ``evoila/meho`` repo was upserting a tenant slug ``rdc-internal``
+ 8 operational conventions extracted from one specific consumer's
``CLAUDE.md`` on every deploy. The seeded conventions flow into the
MCP ``initialize.instructions`` field every agent session receives at
session start (G7.1-T4 #316), so adopting customer B's MCP clients
were receiving customer A's internal operational discipline + repo
references as their operating instructions.

Alembic migrations are forward-only. Migration ``0018`` is preserved
in git history (it is part of the OSS record); this revision sits on
top of it and:

1. **Cleans up the seeded ``rdc-internal`` rows** on existing deploys.
   The cleanup narrows on ``created_by_sub`` / ``actor_sub`` =
   ``'migration:seed-rdc-conventions'`` so an operator who edited a
   seeded convention post-seed keeps their content; only the rows
   ``0018`` itself authored are removed. The ``rdc-internal`` tenant
   row is intentionally preserved -- other v0.2 features (targets,
   audit rows, broadcast overrides, agent definitions) key on
   ``tenant_id``; deleting the tenant would invisibly orphan that
   data. The narrower "remove only the rows the seed authored"
   contract is the safer reversal, mirroring ``0018.downgrade()``.

2. **Seeds a generic ``default`` tenant + 2 illustrative conventions**
   that demonstrate the ``tenant_conventions`` feature without baking
   in a specific consumer's identity. The two conventions read as
   documentation rather than as someone's operational rules -- they
   describe the convention surface itself (slug naming, the
   operator-facing nature of the surface) so an OSS operator looking
   at a fresh deploy can see what the feature does without inheriting
   another customer's CLAUDE.md content.

The deletion happens unconditionally and is idempotent: on a deploy
that ran ``0018`` the seeded rows are removed; on a fresh deploy that
ran ``0018`` immediately before this revision in the same
``upgrade head`` sequence, the cleanup removes the rows ``0018`` just
inserted (intentional -- the OSS-leak guarantee is what matters, not
saving a few DELETE statements at deploy time). The total cost is on
the order of milliseconds against either dialect.

Why a new revision instead of editing ``0018``
----------------------------------------------

Alembic migrations are forward-only by design: an existing deploy
that already ran ``0018`` has the consumer-specific rows in its DB,
and a no-data edit to ``0018``'s body would not remove them on
``upgrade head`` (Alembic gates on the revision number, not the
migration content). The forward-compatible fix is a new revision
whose ``upgrade()`` performs the cleanup + new seed. ``0018``'s file
stays in the OSS history as a record of what was once shipped.

Consumer-side migration for the rdc-internal-specific seed
----------------------------------------------------------

The 8 ``rdc-internal``-specific conventions migrate to a consumer-
side migration template the consumer applies post-deploy in their
own repository (``claude-rdc-hetzner-dc``). That keeps the operational
discipline content alive for the consumer who originally authored it
while removing it from the public OSS surface. See
`docs/architecture/conventions-seed.md <../../../docs/architecture/conventions-seed.md>`_
for the consumer-side template + reapplication path.

The 2 default conventions
-------------------------

* ``slug-naming-kebab-case`` (priority=50) -- documents the slug
  naming convention the feature itself uses, so an operator authoring
  their first convention sees the shape they should follow.
* ``conventions-are-operator-facing`` (priority=10) -- documents the
  operator-facing nature of the convention surface, mirroring decision
  #4's partition between operational rules (server-side) and
  repo-discipline rules (consumer-side).

Both bodies are intentionally short -- under ~200 characters each --
to fit comfortably under the 600-token preamble budget T4's assembler
uses (the budget is per-tenant in aggregate, not per-entry, but the
short bodies leave headroom for operators adding their own
conventions on top).

Dialect-portability decisions
-----------------------------

Mirrors migration ``0018``'s discipline:

* ``gen_random_uuid()`` is PG-only; SQLite uses Python-side
  :func:`uuid.uuid4` because raw SQL with ``ON CONFLICT ... RETURNING``
  needs a literal UUID parameter on SQLite.
* ``now()`` is PG-only; SQLite gets a Python-side
  :func:`datetime.now` ISO-8601 timestamp.
* UUIDs bind as :class:`uuid.UUID` on PG and as the 32-char hex form
  (``value.hex``) on SQLite -- the same shape SQLAlchemy's
  :class:`~sqlalchemy.types.Uuid` column type uses on each dialect.
* All statements are parameterised; no f-string interpolation.

Reversibility contract
----------------------

``downgrade()`` removes the 2 seeded ``default`` conventions + their
seed-authored history rows. The ``default`` tenant row is preserved
on downgrade (same rationale as ``0018``'s downgrade -- other v0.2
features may key on ``tenant_id``). The downgrade does **not**
restore the ``rdc-internal`` seed -- restoring it would re-leak
consumer content into the public deploy. Operators who need the
rdc-internal seed back apply the consumer-side template documented
in ``docs/architecture/conventions-seed.md``.

Cross-references
----------------

* Parent Initiative #1130 (G0.13) -- v0.6.0 dogfood hardening.
* Original task #317 (G7.1-T5) -- the seed migration this revision
  supersedes.
* Original migration:
  ``backend/alembic/versions/0018_seed_rdc_internal_conventions.py``.
* Updated docs: ``docs/architecture/conventions-seed.md``.
* :mod:`tests.test_alembic_seed_0025_supersede` -- behavioural
  coverage (cleanup, new seed, fresh-DB, idempotency, downgrade).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The synthetic ``sub`` claim ``0018`` recorded on every seeded row.
#: Used here as the narrowing predicate for the cleanup step -- only
#: rows ``0018`` itself authored are removed.
_LEGACY_SEED_ACTOR_SUB: Final[str] = "migration:seed-rdc-conventions"

#: The slug ``0018`` used for the consumer-specific tenant.
_LEGACY_TENANT_SLUG: Final[str] = "rdc-internal"

#: The 8 slugs ``0018`` seeded under the legacy tenant. Narrowing the
#: cleanup on this list keeps an operator-curated convention under
#: an unrelated slug safe even if it happened to carry the legacy
#: actor_sub (defence-in-depth -- operator edits should not carry
#: the migration-shaped sub, but if they did, narrowing on the slug
#: list keeps the cleanup scoped).
_LEGACY_SEEDED_SLUGS: Final[tuple[str, ...]] = (
    "vault-canonical",
    "naming-rule-no-ai-tool-names",
    "secret-handling-discipline",
    "cli-wrapper-fallback-discipline",
    "destructive-ops-probe-first",
    "audit-trail-discipline",
    "sensitive-lab-specifics-stay-private",
    "approval-workflow-when-it-lands",
)


#: The synthetic ``sub`` claim recorded on every row this revision
#: authors. Distinct from the legacy ``0018`` marker so the audit
#: trail can distinguish "seeded by 0018" from "seeded by 0025" rows
#: (relevant only on test deploys that downgraded past this revision
#: and replayed).
_SEED_ACTOR_SUB: Final[str] = "migration:seed-default-conventions"


#: The generic tenant slug + display name this revision seeds.
_TENANT_SLUG: Final[str] = "default"
_TENANT_NAME: Final[str] = "Default Tenant (example conventions)"


#: Two illustrative conventions. Read as documentation about the
#: feature itself, not as operational rules a specific operator must
#: follow. Each tuple is ``(slug, title, priority, body)``.
_SEED_CONVENTIONS: Final[tuple[tuple[str, str, int, str], ...]] = (
    (
        "slug-naming-kebab-case",
        "Convention slugs use kebab-case",
        50,
        (
            "Convention slugs are kebab-case identifiers (lowercase "
            "ASCII letters, digits, hyphens). The slug appears in "
            "operator-facing URLs and CLI verbs, so a stable, "
            "human-readable shape matters. This is an illustrative "
            "default convention -- operators replace it with their "
            "own operational discipline via the conventions API."
        ),
    ),
    (
        "conventions-are-operator-facing",
        "Conventions are operator-facing and tenant-scoped",
        10,
        (
            "Tenant conventions are operator-curated operational "
            "rules surfaced to MCP clients via the session "
            "initialize.instructions field. Repo-discipline rules "
            "stay in the consuming application's own repository "
            "(e.g. CLAUDE.md). This is an illustrative default -- "
            "see docs/architecture/conventions-seed.md for the "
            "operational vs. repo-discipline partition."
        ),
    ),
)


def _coerce_uuid(value: object) -> uuid.UUID:
    """Coerce a DB-returned UUID-shaped value to :class:`uuid.UUID`.

    PG returns a :class:`uuid.UUID` instance; SQLite via aiosqlite
    returns the same value as a :class:`str` (``CHAR(32)`` storage
    class). Mirrors ``0018``'s helper.
    """
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _uuid_param(value: uuid.UUID, *, is_postgres: bool) -> object:
    """Return the right Python type for a UUID bind parameter on each dialect.

    Mirrors ``0018``'s helper: PG accepts :class:`uuid.UUID` directly;
    SQLite needs the 32-char hex form (``value.hex``) because
    SQLAlchemy's :class:`~sqlalchemy.types.Uuid` column type stores
    UUIDs as ``CHAR(32)`` on SQLite. Writing the 36-char canonical
    form would silently produce a row whose ``id`` is invisible to
    ORM-issued FK joins.
    """
    return value if is_postgres else value.hex


def _cleanup_legacy_rdc_internal_seed(
    bind: sa.Connection,
    *,
    is_postgres: bool,
) -> None:
    """Remove the rows ``0018`` authored, keep the tenant row.

    Idempotent: when no ``rdc-internal`` tenant exists (e.g. a deploy
    that never ran ``0018`` because the install was bootstrapped at
    head from a future point), the function returns without
    side-effects. When the tenant exists but the seeded rows were
    already cleaned (this revision replayed), the DELETEs match zero
    rows -- still a no-op.
    """
    legacy_tenant_id_row = bind.execute(
        sa.text("SELECT id FROM tenant WHERE slug = :slug"),
        {"slug": _LEGACY_TENANT_SLUG},
    ).scalar()
    if legacy_tenant_id_row is None:
        return
    legacy_tenant_id = _coerce_uuid(legacy_tenant_id_row)

    # Resolve the seeded convention ids first; the history delete
    # narrows on ``convention_id`` AND ``actor_sub``, so operator-
    # authored history entries against the same conventions survive.
    convention_id_rows = bind.execute(
        sa.text(
            """
            SELECT id FROM tenant_conventions
            WHERE tenant_id = :tenant_id AND slug IN :slugs
            """,
        ).bindparams(sa.bindparam("slugs", expanding=True)),
        {
            "tenant_id": _uuid_param(legacy_tenant_id, is_postgres=is_postgres),
            "slugs": list(_LEGACY_SEEDED_SLUGS),
        },
    ).all()
    convention_ids = [_coerce_uuid(row[0]) for row in convention_id_rows]

    if convention_ids:
        # History first -- narrow on ``actor_sub`` so operator-curated
        # PATCH-shape edits against seeded conventions survive.
        bind.execute(
            sa.text(
                """
                DELETE FROM tenant_convention_history
                WHERE convention_id IN :ids
                  AND actor_sub = :actor_sub
                """,
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {
                "ids": [_uuid_param(cid, is_postgres=is_postgres) for cid in convention_ids],
                "actor_sub": _LEGACY_SEED_ACTOR_SUB,
            },
        )
        # Then the convention rows themselves, narrowed on
        # ``created_by_sub`` so an operator-authored convention under
        # one of the seeded slugs survives.
        bind.execute(
            sa.text(
                """
                DELETE FROM tenant_conventions
                WHERE tenant_id = :tenant_id
                  AND slug IN :slugs
                  AND created_by_sub = :created_by_sub
                """,
            ).bindparams(sa.bindparam("slugs", expanding=True)),
            {
                "tenant_id": _uuid_param(legacy_tenant_id, is_postgres=is_postgres),
                "slugs": list(_LEGACY_SEEDED_SLUGS),
                "created_by_sub": _LEGACY_SEED_ACTOR_SUB,
            },
        )

    # The ``rdc-internal`` tenant row itself is intentionally
    # preserved -- see module docstring "Cleans up the seeded
    # rdc-internal rows" rationale (other v0.2 features key on
    # tenant_id; dropping the tenant would orphan that data).


def _upsert_default_tenant(
    bind: sa.Connection,
    *,
    now: datetime,
    is_postgres: bool,
) -> uuid.UUID:
    """Upsert the ``default`` tenant and return its id.

    Same shape as ``0018``'s tenant upsert: ``INSERT ... ON CONFLICT
    (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id``. If an
    operator already created a tenant with slug ``'default'``, the
    upsert refreshes the display name and returns the existing id; the
    conventions seed proceeds against that id.
    """
    new_id = uuid.uuid4()
    stmt_text = sa.text(
        """
        INSERT INTO tenant (id, slug, name, created_at)
        VALUES (:id, :slug, :name, :created_at)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
    )
    result = bind.execute(
        stmt_text,
        {
            "id": _uuid_param(new_id, is_postgres=is_postgres),
            "slug": _TENANT_SLUG,
            "name": _TENANT_NAME,
            "created_at": now,
        },
    )
    return _coerce_uuid(result.scalar_one())


def upgrade() -> None:
    """Clean up legacy ``rdc-internal`` seed, then seed ``default`` tenant.

    Order of operations:

    1. Delete the rows ``0018`` authored against the ``rdc-internal``
       tenant. Idempotent + narrowed on the seed actor_sub so operator
       edits survive.
    2. Upsert the ``default`` tenant row (returns its id).
    3. Insert the 2 illustrative conventions with ``ON CONFLICT
       (tenant_id, slug) DO NOTHING RETURNING id``.
    4. For each convention id returned in step 3, insert one
       CREATE-shape history row.

    Steps 2-4 mirror ``0018``'s upgrade shape verbatim (only the
    tenant slug + convention content differ) so the same idempotency
    and operator-curation guarantees apply.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now = datetime.now(UTC)

    _cleanup_legacy_rdc_internal_seed(bind, is_postgres=is_postgres)

    tenant_id = _upsert_default_tenant(
        bind,
        now=now,
        is_postgres=is_postgres,
    )

    convention_insert = sa.text(
        """
        INSERT INTO tenant_conventions (
            id, tenant_id, slug, title, body, kind, priority,
            created_by_sub, created_at, updated_at
        ) VALUES (
            :id, :tenant_id, :slug, :title, :body, :kind, :priority,
            :created_by_sub, :created_at, :updated_at
        )
        ON CONFLICT (tenant_id, slug) DO NOTHING
        RETURNING id
        """,
    )
    history_insert = sa.text(
        """
        INSERT INTO tenant_convention_history (
            id, convention_id, body_before, body_after, actor_sub,
            ts, audit_id
        ) VALUES (
            :id, :convention_id, :body_before, :body_after,
            :actor_sub, :ts, :audit_id
        )
        """,
    )

    for slug, title, priority, body in _SEED_CONVENTIONS:
        convention_id = uuid.uuid4()
        result = bind.execute(
            convention_insert,
            {
                "id": _uuid_param(convention_id, is_postgres=is_postgres),
                "tenant_id": _uuid_param(tenant_id, is_postgres=is_postgres),
                "slug": slug,
                "title": title,
                "body": body,
                "kind": "operational",
                "priority": priority,
                "created_by_sub": _SEED_ACTOR_SUB,
                "created_at": now,
                "updated_at": now,
            },
        )
        # Skip the history write for rows the operator already owns --
        # mirrors ``0018``'s gate.
        landed = result.scalar()
        if landed is None:
            continue
        bind.execute(
            history_insert,
            {
                "id": _uuid_param(uuid.uuid4(), is_postgres=is_postgres),
                "convention_id": _uuid_param(
                    convention_id,
                    is_postgres=is_postgres,
                ),
                "body_before": None,
                "body_after": body,
                "actor_sub": _SEED_ACTOR_SUB,
                "ts": now,
                "audit_id": None,
            },
        )


def downgrade() -> None:
    """Remove the ``default`` seed rows; keep the tenant row.

    The ``default`` tenant row is **intentionally preserved** on
    downgrade (same rationale as ``0018``: other v0.2 features may
    key on ``tenant_id``).

    The downgrade does **not** restore the ``rdc-internal`` seed --
    restoring it would re-leak consumer content into the public
    deploy. Operators who need the rdc-internal seed back apply the
    consumer-side template documented in
    ``docs/architecture/conventions-seed.md``.

    The history-row delete is narrowed on
    ``actor_sub='migration:seed-default-conventions'`` so operator-
    curated history entries against seeded slugs (a PATCH applied
    post-seed) survive.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    tenant_id_row = bind.execute(
        sa.text("SELECT id FROM tenant WHERE slug = :slug"),
        {"slug": _TENANT_SLUG},
    ).scalar()
    if tenant_id_row is None:
        return
    tenant_id = _coerce_uuid(tenant_id_row)

    seeded_slugs = [slug for slug, _title, _priority, _body in _SEED_CONVENTIONS]

    convention_id_rows = bind.execute(
        sa.text(
            """
            SELECT id FROM tenant_conventions
            WHERE tenant_id = :tenant_id AND slug IN :slugs
            """,
        ).bindparams(sa.bindparam("slugs", expanding=True)),
        {
            "tenant_id": _uuid_param(tenant_id, is_postgres=is_postgres),
            "slugs": seeded_slugs,
        },
    ).all()
    convention_ids = [_coerce_uuid(row[0]) for row in convention_id_rows]

    if convention_ids:
        bind.execute(
            sa.text(
                """
                DELETE FROM tenant_convention_history
                WHERE convention_id IN :ids
                  AND actor_sub = :actor_sub
                """,
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {
                "ids": [_uuid_param(cid, is_postgres=is_postgres) for cid in convention_ids],
                "actor_sub": _SEED_ACTOR_SUB,
            },
        )
        bind.execute(
            sa.text(
                """
                DELETE FROM tenant_conventions
                WHERE tenant_id = :tenant_id
                  AND slug IN :slugs
                  AND created_by_sub = :created_by_sub
                """,
            ).bindparams(sa.bindparam("slugs", expanding=True)),
            {
                "tenant_id": _uuid_param(tenant_id, is_postgres=is_postgres),
                "slugs": seeded_slugs,
                "created_by_sub": _SEED_ACTOR_SUB,
            },
        )

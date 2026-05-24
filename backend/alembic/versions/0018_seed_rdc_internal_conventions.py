# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seed the ``rdc-internal`` tenant + 8 operational conventions.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-24

Initiative #229 (G7.1 Tenant conventions + Layer 2 starter), Task
#317 (T5). This is a **data migration** -- no schema changes -- that
turns the conventions feature into something usable for the
``rdc-internal`` tenant by seeding the initial content extracted from
the consumer's ``CLAUDE.md`` per `decision #4
<../../../docs/planning/v0.2-decisions.md>`_ (operational rules only;
repo-discipline rules stay in the consumer's repo).

T1 (#313) created ``tenant_conventions`` + ``tenant_convention_history``;
T2 (#314) shipped the API; T4 (#316) wires the preamble assembler.
Without seeded rows the assembler returns empty, the
:data:`meho://tenant/<id>/conventions` MCP resource lists nothing, and
the spec-optional ``instructions`` field on the MCP ``initialize``
response carries no tenant-specific operating discipline. This
migration closes that gap with a one-shot, idempotent, reversible
seed.

What it does
------------

1. **Upserts the ``rdc-internal`` tenant.** ``INSERT ... ON CONFLICT
   (slug) DO UPDATE SET name = EXCLUDED.name`` -- if an operator
   already created the row manually (chassis-era ad-hoc bootstrap,
   prior dev fixture, etc.) the migration keeps that row's ``id``
   intact and refreshes the display name. The slug
   ``'rdc-internal'`` is the unique key; the named ``tenant_slug_idx``
   migration ``0002`` declared is the ``ON CONFLICT`` target.

2. **Inserts 8 ``operational`` conventions for that tenant.**
   ``INSERT ... ON CONFLICT (tenant_id, slug) DO NOTHING`` -- a
   convention an operator already authored (post-T2, via the API)
   takes precedence over the seed body; the migration never
   overwrites operator-curated rules. The ``ON CONFLICT`` target is
   the named composite ``tenant_conventions_tenant_slug_idx``
   migration ``0015`` declared.

3. **Inserts one CREATE-shape history row per seeded convention.**
   ``body_before=NULL`` (no prior state on initial seed),
   ``body_after=<seed body>``, ``actor_sub='migration:seed-rdc-conventions'``,
   ``audit_id=NULL`` (no audit_log row to point at -- the migration
   runs outside any HTTP request, and ``audit_log.tenant_id`` itself
   is nullable in v0.2 per migration ``0002``'s ``audit_log`` column
   addition). The history row is inserted **only when the convention
   insert actually landed** (i.e. ``ON CONFLICT DO NOTHING`` returned
   the new id) -- skipping the history write for rows the operator
   already owns avoids polluting their edit trail with a synthetic
   "seed" entry that never happened from their perspective.

The 8 conventions
-----------------

Per `decision #4 <../../../docs/planning/v0.2-decisions.md>`_'s explicit
list (Vault canonical, naming rule, secret-handling, CLI-wrapper
fallback, sensitive-lab-specifics-stay-private) plus three
operational rules pulled forward from cross-team operating practice
(destructive-ops-probe-first, audit-trail-discipline,
approval-workflow-when-it-lands). Every body is treated as
**untrusted content** when T4's preamble assembler packs it into the
session preamble -- the wrapper guard string (T4) and the convention
body never agree to override MEHO's policy / audit / approval
enforcement. The slug → source paragraph mapping ships alongside this
migration in
`docs/architecture/conventions-seed.md <../../../docs/architecture/conventions-seed.md>`_.

Why ``kind='operational'`` for all 8
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Per decision #4 the G7 server-side partition migrates *only*
operational rules. ``workflow`` and ``reference`` kinds are out of
scope for the seed because (a) the consumer's repo CLAUDE.md doesn't
carry workflow / reference content the seed could mirror, and (b)
T4's preamble assembler packs **only** ``kind='operational'``
conventions -- a ``workflow`` rule landed via this seed would be
invisible to the session preamble surface T5 exists to populate.

Priority assignment
~~~~~~~~~~~~~~~~~~~

T4 (#316) packs ``operational`` conventions highest-``priority``-first
into a 600-token budget; over-budget entries are dropped whole. The
seed assigns three priority tiers that reflect operator safety
ranking:

* ``priority=100`` -- secrets + sensitive lab specifics. These are the
  rules whose breach is unrecoverable (rotated-secret-or-compromised-
  tenant). They must reach every session even when the budget is
  tight.
* ``priority=50`` -- naming + CLI-wrapper + destructive-ops-probe.
  Convention-and-discipline rules whose breach is recoverable but
  costly (incoherent target identifiers; deleted-wrapper-with-no-
  meho-replacement; collision on a destructive op). Important but
  drop-when-tight.
* ``priority=10`` -- audit-trail + approval-workflow-when-it-lands.
  Aspirational / forward-looking; drop first when the budget runs out
  because the rules' enforcement substrate either lands in G4 or is
  not yet built.

The rationale and exact slug → priority mapping ship in
`docs/architecture/conventions-seed.md <../../../docs/architecture/conventions-seed.md>`_.

Idempotency
-----------

Re-running the migration is a no-op for already-seeded data:

* The tenant ``ON CONFLICT (slug) DO UPDATE`` is safe to re-run; the
  ``EXCLUDED.name`` is the same string the previous pass wrote.
* The convention ``ON CONFLICT (tenant_id, slug) DO NOTHING`` skips
  every already-present row; nothing is written.
* The history-row insert is gated on the conventions ``RETURNING id``
  result -- only convention inserts that actually landed produce a
  history row, so a re-run skips history writes for already-seeded
  conventions too.

This matters for the test suite (upgrade → downgrade → upgrade replay)
and for the testcontainers PG replay cycle in
:mod:`tests.test_migration_rollback`.

Reversibility contract
----------------------

``downgrade()`` deletes the seeded conventions by slug (and their
history rows by ``convention_id``). The ``rdc-internal`` **tenant
row is intentionally preserved** on downgrade because (a) operators
may have configured tenant-scoped data (targets, audit rows,
broadcast overrides) keyed by ``tenant_id``, and (b) deleting the
tenant would cascade-orphan all that data (no actual FK cascade --
the columns are soft FKs -- but the rows would be invisibly
dangling). The narrower "remove only the rows this migration
created" contract is the safer reversibility shape.

The history row delete is keyed on
``actor_sub='migration:seed-rdc-conventions'`` and the seeded slug
list -- operator-curated history entries (a subsequent PATCH against
a seeded slug) survive the downgrade unharmed.

Dialect-portability decisions
-----------------------------

Mirrors the discipline migration ``0011`` (backfill data migration)
established:

* ``gen_random_uuid()`` is PG-only; SQLite uses Python-side
  :func:`uuid.uuid4` because raw SQL with ``ON CONFLICT ... RETURNING``
  needs a literal UUID parameter on SQLite (no ``gen_random_uuid()``).
  The migration pre-mints the ``rdc-internal`` tenant id and each
  convention id Python-side, passes them as bind parameters, and
  lets PG's server default apply only when the row already exists
  (in which case the ``ON CONFLICT DO UPDATE`` / ``DO NOTHING``
  short-circuits before the default fires anyway).
* ``now()`` is PG-only; SQLite gets a literal ISO-8601 timestamp via
  :func:`datetime.now`. The
  ``tenant_conventions.created_at`` / ``updated_at`` and
  ``tenant_convention_history.ts`` columns are NOT NULL on every
  dialect, so passing a literal Python timestamp keeps both engines
  happy.
* The migration uses parameterised statements (``:slug``, ``:body``)
  rather than f-string interpolation -- prevents SQL-injection-shaped
  bugs even though every value is a compile-time constant in the
  migration, and keeps the audit trail of "what got inserted" readable
  when the migration is replayed under PG ``log_statement=all``.

Cross-references
----------------

* Initiative #229 (G7.1) -- the tenant conventions + Layer 2 starter
  rollup. Decision #4 (``docs/planning/v0.2-decisions.md``) is the
  authoritative partition between operational rules (migrated here)
  and repo-discipline rules (stay in the consumer's CLAUDE.md).
* T1 (#313) -- created the two tables this migration writes to.
* T2 (#314) -- shipped the API operators use to add / edit / remove
  conventions post-seed.
* T4 (#316) -- the session-preamble assembler that turns the seeded
  rows into MCP ``initialize.instructions`` content.
* ``backend/alembic/versions/0011_backfill_operation_group_when_to_use.py``
  -- the prior-art data-migration shape this file mirrors
  (self-contained ``sa.table`` shims, Python-side timestamp, no ORM
  import).
* ``docs/architecture/conventions-seed.md`` -- slug → source paragraph
  → priority mapping table.
* :mod:`tests.test_alembic_seed_rdc_conventions` -- behavioural
  coverage (idempotency, downgrade-keeps-tenant, all 8 conventions
  land with the expected priorities).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The synthetic ``sub`` claim recorded on every seeded row's
#: ``created_by_sub`` (on ``tenant_conventions``) and on every
#: matching history row's ``actor_sub`` (on
#: ``tenant_convention_history``). G8's audit-query path can grep on
#: this exact string to find seed-vs-operator-authored content, and
#: the ``downgrade()`` predicate uses it as the narrowing key when
#: deleting history rows so operator-curated history survives.
_SEED_ACTOR_SUB: Final[str] = "migration:seed-rdc-conventions"


#: The tenant the seed targets. Slug is the unique natural key (the
#: ``tenant_slug_idx`` from migration ``0002``); name is the
#: operator-facing display label.
_TENANT_SLUG: Final[str] = "rdc-internal"
_TENANT_NAME: Final[str] = "RDC Internal"


#: The 8 operational conventions, in deterministic source-order so the
#: migration's replay log is stable across runs. Each tuple is
#: ``(slug, title, priority, body)``. Body text is taken verbatim
#: from the issue body (`#317 <https://github.com/evoila/meho/issues/317>`_).
#:
#: Priority assignment rationale lives in
#: ``docs/architecture/conventions-seed.md``. Briefly: 100 for rules
#: whose breach is unrecoverable (secrets); 50 for rules whose breach
#: is recoverable but costly (naming, wrappers, destructive ops); 10
#: for forward-looking aspirational rules (audit-trail discipline,
#: approval workflow).
_SEED_CONVENTIONS: Final[tuple[tuple[str, str, int, str], ...]] = (
    (
        "vault-canonical",
        "Vault is canonical for secrets",
        100,
        (
            "Vault is the canonical secret store. 1Password is "
            "bootstrap-residual only (Vault unseal shares + initial "
            "admin userpass). Never write to 1Password; always read "
            "from Vault. Pipe secrets directly into the consuming "
            "command -- never paste into chat, never commit to repos."
        ),
    ),
    (
        "naming-rule-no-ai-tool-names",
        "No AI-tool names in operator-visible identifiers",
        50,
        (
            "Operator-visible identifiers (target names, hostnames, "
            "IPs, ticket titles, lab object names) must never contain "
            "AI-tool names like 'claude', 'gpt', or other model-shaped "
            "tokens. The lab program's Holodeck `claude` legacy is the "
            "exception -- operator-visible refs renamed; VM-internal "
            "refs are stuck pending destroy+redeploy. New names start "
            "clean."
        ),
    ),
    (
        "secret-handling-discipline",
        "Secret-handling discipline",
        100,
        (
            "Secrets never enter chat windows, never enter commit "
            "messages, never enter pull request bodies. Pipe directly "
            "from `meho vault kv read ...` into the consuming "
            "command. If a secret accidentally leaks into "
            "chat/commit/PR, rotate immediately, then refile the work."
        ),
    ),
    (
        "cli-wrapper-fallback-discipline",
        "CLI wrapper fallback during MEHO transition",
        50,
        (
            "While MEHO is in transition (v0.2), existing "
            "`scripts/*.sh` wrappers remain available as fallback. "
            "Never delete a wrapper until its `meho` equivalent has "
            "been in daily use for >= 2 weeks against real targets. "
            "Document the wrapper-to-meho mapping in the PR "
            "description."
        ),
    ),
    (
        "destructive-ops-probe-first",
        "Probe before destructive ops",
        50,
        (
            "Before any destructive operation (vm.destroy, "
            "dns.zone.delete, k8s.namespace.delete), run `meho "
            "topology dependents <resource>` (when G9 lands) or the "
            "equivalent inventory check (G3.1's `vsphere.vm.info`). "
            "Document what you'd touch in the ticket. Confirm with "
            "the team via `meho status --watch` that no one else is "
            "operating on the same resource."
        ),
    ),
    (
        "audit-trail-discipline",
        "Document findings as you go",
        10,
        (
            "Every diagnostic or investigative session captures "
            "findings as kb entries. `meho kb write` (when G4 lands) "
            "is the canonical path. Until G4, write to local `kb/` "
            "and import in the next sync."
        ),
    ),
    (
        "sensitive-lab-specifics-stay-private",
        "Sensitive lab specifics stay private",
        100,
        (
            "The `evoila/meho` repo is public. Internal lab "
            "specifics (real hostnames, IPs, customer names, project "
            "codes) must not appear in commits, issues, or public "
            "docs. Generic naming + sanitised examples only. Internal "
            "coordination happens in `evoila-bosnia/meho-internal` or "
            "other private channels."
        ),
    ),
    (
        "approval-workflow-when-it-lands",
        "Approval workflow expectations",
        10,
        (
            "When approval workflow lands (v0.2.next G-Policy), "
            "destructive ops on production targets will block waiting "
            "for a second operator's approval. Until then, operate by "
            "team norm: discuss in #ops Slack before destructive prod "
            "ops; document in the ticket."
        ),
    ),
)


def _tenant_table() -> sa.TableClause:
    """Return a Core ``sa.table`` shim for ``tenant``.

    Self-contained per the Alembic data-migration cookbook -- never
    imports the ORM model (which would pin the migration to one
    moment in the schema's history and break under future column
    adds). Mirrors the discipline migration ``0011`` follows for the
    ``operation_group`` data migration.
    """
    return sa.table(
        "tenant",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.Text()),
        sa.column("name", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )


def _convention_table() -> sa.TableClause:
    """Return a Core ``sa.table`` shim for ``tenant_conventions``."""
    return sa.table(
        "tenant_conventions",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("slug", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("body", sa.Text()),
        sa.column("kind", sa.Text()),
        sa.column("priority", sa.SmallInteger()),
        sa.column("created_by_sub", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _history_table() -> sa.TableClause:
    """Return a Core ``sa.table`` shim for ``tenant_convention_history``."""
    return sa.table(
        "tenant_convention_history",
        sa.column("id", sa.Uuid()),
        sa.column("convention_id", sa.Uuid()),
        sa.column("body_before", sa.Text()),
        sa.column("body_after", sa.Text()),
        sa.column("actor_sub", sa.Text()),
        sa.column("ts", sa.DateTime(timezone=True)),
        sa.column("audit_id", sa.Uuid()),
    )


def _coerce_uuid(value: object) -> uuid.UUID:
    """Coerce a DB-returned UUID-shaped value to :class:`uuid.UUID`.

    PG returns a :class:`uuid.UUID` instance; SQLite via aiosqlite
    returns the same value as a :class:`str` (``CHAR(32)`` storage
    class). Either way callers need a single uniform type to pass back
    as bind parameters, so this helper handles the dialect drift in
    one place.
    """
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _uuid_param(value: uuid.UUID, *, is_postgres: bool) -> object:
    """Return the right Python type for a UUID bind parameter on each dialect.

    On PG the driver accepts either :class:`uuid.UUID` or :class:`str`;
    on SQLite via aiosqlite a raw :class:`uuid.UUID` trips
    ``ProgrammingError: type 'UUID' is not supported`` because the
    sqlite3 stdlib driver does not register a UUID adapter by default.
    Passing the hex form (``str(uuid_value)``) is the dialect-portable
    shape -- mirrors the same string-cast discipline the existing
    test fixtures (e.g. ``tests.test_migration_0011_backfill_when_to_use``)
    apply when issuing raw-SQL inserts against SQLite.
    """
    return value if is_postgres else str(value)


def _upsert_rdc_internal_tenant(
    bind: sa.Connection,
    *,
    now: datetime,
    is_postgres: bool,
) -> uuid.UUID:
    """Upsert the ``rdc-internal`` tenant and return its id.

    Issues a single ``INSERT ... ON CONFLICT (slug) DO UPDATE SET name
    = EXCLUDED.name RETURNING id``. Both PG and SQLite (3.24+) accept
    this exact shape; the ``ON CONFLICT`` target is the named
    ``tenant_slug_idx`` migration ``0002`` declared.

    The tenant id is minted Python-side (``uuid.uuid4()``) rather than
    relying on the PG ``gen_random_uuid()`` server default because
    SQLite has no such default and the migration test suite runs
    against SQLite. PG honours the bind parameter the same way it
    would honour the server default -- both produce a fresh UUID,
    and the ``ON CONFLICT DO UPDATE`` path returns the existing row's
    id either way.
    """
    tenant = _tenant_table()
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
    tenant_id = _coerce_uuid(result.scalar_one())
    # Silence the unused-table warning -- the shim is built for
    # symmetry with the other table accessors even when the explicit
    # text-statement path doesn't reference it.
    _ = tenant
    return tenant_id


def upgrade() -> None:
    """Seed the ``rdc-internal`` tenant + 8 operational conventions.

    Order of operations:

    1. Upsert the tenant row (returns the tenant id, whether the row
       was created or pre-existed).
    2. For each convention tuple: ``INSERT ... ON CONFLICT (tenant_id,
       slug) DO NOTHING RETURNING id``. The ``RETURNING`` clause
       yields the new id only when the insert actually landed.
    3. For every convention id returned in step 2, insert one
       CREATE-shape history row.

    All three steps share a single ``now`` timestamp so the seeded
    rows carry a coherent ``created_at`` / ``updated_at`` / ``ts`` --
    mirrors the ORM's per-write default-factory discipline without
    importing the model.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now = datetime.now(UTC)

    tenant_id = _upsert_rdc_internal_tenant(
        bind,
        now=now,
        is_postgres=is_postgres,
    )

    convention = _convention_table()
    history = _history_table()

    # Build the per-row INSERT once outside the loop; each iteration
    # re-binds parameters. ``ON CONFLICT (tenant_id, slug) DO NOTHING
    # RETURNING id`` is the upsert-or-skip shape -- when the row
    # already exists, ``RETURNING id`` yields no rows and the
    # history-write branch is skipped.
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
        # ``ON CONFLICT DO NOTHING RETURNING id`` returns zero rows
        # when the conflict fires (the operator-authored row stays
        # put). Skip the matching history write in that case --
        # synthesising a "seed" history entry for an operator-owned
        # row would lie about the row's lineage.
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
    # Silence the unused-table warning -- the shims are built for
    # symmetry with the other migrations even when the explicit
    # text-statement path doesn't reference them directly.
    _ = (convention, history)


def downgrade() -> None:
    """Delete the seeded conventions + history rows; keep the tenant.

    The ``rdc-internal`` tenant row is **intentionally preserved** on
    downgrade. Other v0.2 features (targets, audit rows, broadcast
    overrides, agent definitions) key on ``tenant_id``; deleting the
    tenant would cascade-orphan that data even with the soft-FK
    discipline (no DB-level CASCADE fires, but the rows would become
    invisibly dangling). The narrower "remove only the rows this
    migration created" contract is the safer reversal.

    The history-row delete narrows on
    ``actor_sub='migration:seed-rdc-conventions'`` so operator-curated
    history entries (a PATCH applied by an operator against a seeded
    slug after the seed migration ran) survive untouched. Same
    discipline migration ``0011``'s ``WHERE when_to_use LIKE``
    predicate follows: rewind only what this migration authored.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Resolve the tenant id first; if the tenant row doesn't exist
    # (operator manually deleted it post-seed, or downgrade runs
    # against a DB that never saw the upgrade), there is nothing to
    # clean up.
    tenant_id_row = bind.execute(
        sa.text("SELECT id FROM tenant WHERE slug = :slug"),
        {"slug": _TENANT_SLUG},
    ).scalar()
    if tenant_id_row is None:
        return
    tenant_id = _coerce_uuid(tenant_id_row)

    seeded_slugs = [slug for slug, _title, _priority, _body in _SEED_CONVENTIONS]

    # Resolve the convention ids the seed authored -- the history
    # delete narrows on ``convention_id`` to avoid touching rows that
    # might belong to other conventions (defence-in-depth; the
    # ``actor_sub`` predicate already narrows on the seed marker but
    # the join keeps the delete scoped to seeded conventions even if
    # an operator later edits one with the same synthetic sub).
    # ``expanding=True`` is the dialect-portable form of "IN (...)" --
    # SQLAlchemy expands the bind parameter into the right number of
    # placeholders at execute-time, working identically on PG and
    # SQLite.
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
        # History first -- by ``actor_sub`` + ``convention_id`` so
        # operator-authored edits survive (operator edits carry a
        # JWT-shaped ``sub``, not the synthetic seed marker).
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
        # Then the convention rows themselves -- by ``(tenant_id,
        # slug)`` so the unique composite index is the access path.
        # Narrows on ``created_by_sub`` so operator-curated
        # conventions (a row the operator authored under the same
        # slug pre-seed and the seed skipped on ``ON CONFLICT DO
        # NOTHING``) survive.
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

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SQLAlchemy 2.x ORM models for the backplane database.

The module exposes the declarative :class:`Base` that every model
inherits from, plus :class:`AuditLog` (v0.1) and :class:`Tenant`
(v0.2 / G0.1). The metadata object on :data:`Base.metadata` is what
:mod:`meho_backplane.alembic.env` imports as ``target_metadata`` so
``alembic revision --autogenerate`` can diff the model graph against
the live schema.

Type-correct ``Mapped[...]`` annotations are non-negotiable per
SQLAlchemy 2.x: the typed-mapped pattern is what gives mypy real
column types instead of ``Any`` and what lets ``DeclarativeBase``
build the column metadata without a separate ``__table_args__`` /
``Column(...)`` recital.

Schema decisions for :class:`Tenant`:

* ``id`` — UUID primary key. Same portable :class:`Uuid` shape the
  audit-log uses; PG production gets a ``gen_random_uuid()``
  server-default via the migration, the ORM falls back to
  ``default=uuid.uuid4`` for the SQLite dev/test driver and for
  out-of-band inserts.
* ``slug`` — Text NOT NULL UNIQUE. The operator-facing identifier
  (``rdc-internal``, ``customer-a``); used in URLs, log lines,
  audit displays. The unique constraint is enforced both at the
  column level (so SQLAlchemy generates the right DDL) and via
  the explicitly-named ``tenant_slug_idx`` b-tree index (stable
  identifier for later migrations to reference).
* ``name`` — Text NOT NULL. Free-form display label
  (e.g. "RDC Internal Tenancy"); not constrained or indexed.
* ``created_at`` — ``timestamptz``. PG-side ``now()`` server
  default via the migration; the ORM also declares
  ``default=lambda: datetime.now(UTC)`` so SQLite dev/test paths
  populate the column without relying on the dialect.

The model deliberately omits a backref from :class:`AuditLog` to
:class:`Tenant`. Two reasons: (a) v0.2's ``audit_log.tenant_id``
ships **without** a FK (see ``0002_create_tenant_and_audit_tenant_id``
docstring for the rationale), and a SQLAlchemy ``relationship()``
without a column-level FK requires an explicit ``primaryjoin`` /
``foreign()`` annotation that makes the model harder to reason
about; (b) the audit middleware's hot path never lazy-loads tenant
metadata — it only writes the FK column. v0.2.next can introduce
the relationship together with the FK tightening.

Schema decisions for :class:`AuditLog`:

* ``id`` — UUID primary key. Declared via SQLAlchemy's portable
  :class:`Uuid` type, which compiles to ``UUID`` on PostgreSQL and
  ``CHAR(32)`` on SQLite. The migration sets a PG-only
  ``gen_random_uuid()`` server-default; the model also declares
  ``default=uuid.uuid4`` so the audit middleware works against the
  SQLite dev/test driver too. PG production technically pays the
  server-default cost only when ``id`` is omitted on insert, which
  the middleware never does — but keeping it allows out-of-band
  inserts (operator backfills) to remain straightforward.
* ``occurred_at`` — ``timestamptz``. Server-default ``now()`` set on
  PG; on SQLite the model default uses ``datetime.now(UTC)``. The
  middleware also assigns this explicitly so the value reflects
  request-completion time on the server, not row-insert time.
* ``operator_sub`` — Text NOT NULL. Indexed (b-tree) so audit-by-
  operator queries don't sequential-scan.
* ``method`` / ``path`` / ``status_code`` — request shape, NOT NULL.
* ``request_id`` — UUID, nullable. Carries the
  :class:`~meho_backplane.middleware.RequestContextMiddleware`
  correlation id when the value parses as a UUID; otherwise NULL.
  Some clients send opaque ``X-Request-Id`` strings (hex, k8s
  request ids); rather than reject those at audit time we drop the
  request_id binding for that single row — the audit insert must
  not fail on a request-shape mismatch.
* ``duration_ms`` — ``numeric(10,2)``, nullable. Echoed from the
  middleware's monotonic timer.
* ``payload`` — JSON column NOT NULL DEFAULT ``{}``. Declared via
  ``JSON().with_variant(JSONB(), "postgresql")`` so PG production
  gets binary JSONB (indexable by ``@>``, GIN-friendly) while
  SQLite dev/test gets the generic JSON type that stores text. v0.1
  always writes ``{}``; v0.2 may capture per-route structured data.
  The column is the forward-compat escape hatch for payload
  evolution without DDL changes.

Indexes:

* ``audit_log_occurred_at_idx`` — DESC b-tree on ``occurred_at`` so
  "last N audit rows" queries (the dominant CLI query shape) hit
  the index instead of sorting the whole table.
* ``audit_log_operator_sub_idx`` — b-tree on ``operator_sub`` so
  "all rows for operator X" queries (compliance + incident-response
  shape) hit the index.
* ``audit_log_tenant_id_idx`` — b-tree on ``tenant_id`` so
  per-tenant audit queries ("show me everything in
  ``rdc-internal``") hit the index. Added by migration ``0002``;
  G0.1 sibling tasks T2/T3 wire the column writes.

References
----------
* https://docs.sqlalchemy.org/en/20/orm/declarative_styles.html
* https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#postgresql-data-types
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

__all__ = ["AuditLog", "Base", "Tenant"]


#: Portable JSON column type — :class:`JSONB` on PostgreSQL (binary
#: JSON, indexable by ``@>`` / GIN), generic :class:`JSON` (text)
#: on every other dialect including the SQLite dev/test path.
_PORTABLE_JSON: TypeEngine[dict[str, object]] = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base for every backplane ORM model.

    Imported by :mod:`meho_backplane.alembic.env` as
    ``target_metadata = Base.metadata`` so Alembic's
    ``--autogenerate`` flow can diff the model graph against the
    live schema. Subclasses register themselves into
    :attr:`Base.metadata` at class-definition time; nothing else
    needs to call into Alembic explicitly.
    """


class AuditLog(Base):
    """One row per authenticated request, written synchronously before response.

    The :class:`~meho_backplane.audit.AuditMiddleware` constructs an
    instance, populates every field from the request / response /
    contextvars, and commits before yielding the response back to
    the ASGI send chain. Read-only by every other consumer in v0.1
    (no UPDATE / DELETE paths exist).

    Field semantics are documented on the module docstring; this
    class deliberately ships with no helper methods — the audit row
    is a write-once record and helper logic belongs in the
    middleware that builds it.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    operator_sub: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
    )
    duration_ms: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    # Nullable on purpose — chassis-era audit rows from before G0.1
    # have no tenant; the column is populated post-G0.1-T3 by the
    # AuditMiddleware reading the contextvar bound from the JWT
    # claim. v0.2.next will tighten to NOT NULL after backfilling.
    # See ``0002_create_tenant_and_audit_tenant_id`` for the FK
    # rationale (none in v0.2 by design).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "audit_log_occurred_at_idx",
            "occurred_at",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_operator_sub_idx",
            "operator_sub",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_tenant_id_idx",
            "tenant_id",
            postgresql_using="btree",
        ),
    )


class Tenant(Base):
    """A tenant of the meho backplane.

    Every authenticated request post-G0.1 carries a ``tenant_id``
    JWT claim that resolves to a row in this table. The slug
    (``rdc-internal``, ``customer-a``) is the operator-facing
    handle; the ``id`` is the FK keystone every per-tenant feature
    (knowledge bases, memory scopes, target registries, broadcast
    streams, audit-log scoping) joins on.

    The model deliberately ships with no helper methods — tenant
    rows are write-mostly (created via the future tenants-CRUD UX
    or seeding migration) and the query patterns are simple enough
    to live at the call site.
    """

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    slug: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "tenant_slug_idx",
            "slug",
            postgresql_using="btree",
        ),
    )

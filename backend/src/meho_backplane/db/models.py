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
  audit displays. Uniqueness is enforced **exclusively** by the
  named ``tenant_slug_idx`` b-tree index (declared
  ``unique=True``); the column itself omits ``unique=True`` so
  PostgreSQL does not auto-create a duplicate unique index next
  to the named one. The named index gives later migrations a
  stable identifier to reference.
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

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = ["AuditLog", "Base", "Document", "Tenant"]


#: Portable JSON column type — :class:`JSONB` on PostgreSQL (binary
#: JSON, indexable by ``@>`` / GIN), generic :class:`JSON` (text)
#: on every other dialect including the SQLite dev/test path.
_PORTABLE_JSON: TypeEngine[dict[str, object]] = JSON().with_variant(JSONB(), "postgresql")


#: Portable 384-dimensional dense-vector column type —
#: :class:`pgvector.sqlalchemy.Vector` (``vector(384)``) on
#: PostgreSQL where the ``vector`` extension is enabled (see
#: migration ``0003``), JSON-encoded ``TEXT`` on every other dialect
#: (SQLite dev/test) via :class:`_PortableVector384`. The 384
#: dimensionality matches the ``BAAI/bge-small-en-v1.5`` model the
#: EmbeddingService in G0.4-T2 (#259) will load by default.
#:
#: The :class:`TypeDecorator` keeps the Python contract
#: ``list[float]`` on **both** dialects: on PG the pgvector adapter's
#: bind/result processors serialize ``list[float]`` ↔ ``vector(384)``
#: natively; on SQLite the decorator JSON-encodes the list on bind
#: and decodes it back on result, so the same ORM call site
#: (``Document(embedding=[0.1, 0.2, ...])``) works against the
#: dev/test driver without ``# type: ignore`` or stringified
#: placeholders. The escape hatch satisfies SQLAlchemy 2.x's typed
#: ``Mapped[list[float]]`` annotation on :attr:`Document.embedding`.
class _PortableVector384(TypeDecorator[list[float]]):
    """Dialect-portable ``vector(384)`` column with a ``list[float]`` Python contract.

    On PostgreSQL the column is the native pgvector type (via
    :meth:`with_variant`); pgvector's own bind/result processors
    handle the ``list[float]`` ↔ ``vector(384)`` round-trip. On every
    other dialect the column compiles to :class:`Text` and the
    :meth:`process_bind_param` / :meth:`process_result_value` hooks
    JSON-encode the list on the way in and decode it on the way back
    so callers see ``list[float]`` regardless of dialect.

    The decorator is :attr:`cache_ok` so SQLAlchemy's query-plan
    cache can hash it — the encoding logic is pure and parameter-
    free, so cache reuse across statements is safe.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self,
        value: list[float] | None,
        dialect: Dialect,
    ) -> str | None:
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(
        self,
        value: str | None,
        dialect: Dialect,
    ) -> list[float] | None:
        if value is None:
            return None
        decoded = json.loads(value)
        return [float(x) for x in decoded]


_PORTABLE_VECTOR_384: TypeEngine[list[float]] = _PortableVector384().with_variant(
    Vector(384), "postgresql"
)


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
    # Uniqueness is enforced exclusively by the named ``tenant_slug_idx``
    # below (declared ``unique=True``). Setting ``unique=True`` on the
    # column too would prompt PostgreSQL to auto-create a second unique
    # index alongside the named one — two structurally identical b-tree
    # indexes maintained on every insert/update of ``tenant`` for zero
    # benefit. One named unique b-tree is enough; later migrations and
    # operators reference it by stable name.
    slug: Mapped[str] = mapped_column(
        Text,
        nullable=False,
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
            unique=True,
            postgresql_using="btree",
        ),
    )


class Document(Base):
    """A retrievable per-tenant document with a dense embedding.

    Shared between G4 (#215, knowledge layer) and G5 (#216, memory
    layer): both ingestion paths write rows here via the
    ``index_document`` helper landed by G0.4-T3 (#260), and the
    ``retrieve`` helper landed by T4 (#261) reads them back through
    hybrid BM25 + cosine RRF. The substrate stays one table, one
    embedding pipeline, one retrieval implementation — splitting it
    across G4 and G5 would fork the model-choice decision and double
    the embedding-compute cost.

    Schema decisions for :class:`Document`:

    * ``id`` — UUID primary key. Same portable :class:`Uuid` shape
      the chassis tables use; PG production gets a
      ``gen_random_uuid()`` server default via migration ``0003``,
      the ORM falls back to ``default=uuid.uuid4`` on SQLite.
    * ``tenant_id`` — UUID NOT NULL with a real ``REFERENCES
      tenant(id)`` FK constraint. Unlike :attr:`AuditLog.tenant_id`
      (chassis-era rows with no real tenant to point at; FK deferred
      to v0.2.next backfill), :class:`Document` is a brand-new table
      with no pre-existing rows and a clean downgrade that drops the
      whole table — there is no backfill or cascade decision to defer.
      Enforcing the FK at the DB layer is the cheapest point to make
      the ownership invariant unbreakable: ``index_document`` (T3)
      cannot silently insert orphan rows for a typo'd / deleted /
      replayed tenant id, and corpus poisoning via a malformed
      contextvar surfaces as an :class:`IntegrityError` at insert
      time instead of as an unreachable row at retrieval time. NOT
      NULL because every document is owned by exactly one tenant and
      tenant-scoped queries are the only retrieval path.
    * ``source`` — Text NOT NULL. Origin namespace (``"kb"``,
      ``"memory"``, ``"docs-sidecar"``, future). One namespace per
      consuming Goal so cross-source filtering is a single column
      lookup, indexed via the composite uniqueness constraint
      together with ``tenant_id`` and ``source_id``.
    * ``source_id`` — Text NOT NULL. The per-source natural-key
      identifier (kb slug, memory file path, etc.). Stored as text
      so different consumers can keep their own identifier
      conventions without a schema change.
    * ``kind`` — Text NOT NULL. Per-source classification
      (``"kb-entry"``, ``"kb-index"``, ``"memory-user"``,
      ``"memory-tenant"``, future). Enables retrieval filters that
      narrow within a source — e.g. ``retrieve(source="memory",
      kind="memory-user")``. Free-form text so consumers add new
      kinds without DDL.
    * ``body`` — Text NOT NULL. The document text — what BM25
      searches, what the embedding is computed from. Stored as-is
      (no chunking in v0.2; chunked retrieval is a v0.2.next
      decision per the Initiative body).
    * ``body_hash`` — Text NOT NULL. SHA-256 hex digest of
      :attr:`body` for change-detection. The ``index_document``
      helper (T3) compares the incoming hash against the existing
      row's and short-circuits the embed step when they match; this
      is the cost optimisation that makes ``meho kb refresh``
      against an unchanged corpus essentially free. Indexed by
      ``documents_body_hash_idx`` so the lookup is a btree probe.
    * ``tokens`` — Integer, nullable. Rough token count populated
      during indexing for budget-tracking by future agent flows
      (G4 + G5 will fill it in via a heuristic, replaceable by
      ``tiktoken`` once we have a model dependency). NULL means
      "not yet estimated"; not a load-bearing retrieval signal.
    * ``embedding`` — :class:`Vector(384)` on PG (via the
      pgvector adapter), :class:`Text` on SQLite (via
      ``with_variant``). NOT NULL — every document must have an
      embedding for cosine retrieval to work; the
      ``index_document`` helper computes one synchronously before
      committing.
    * ``doc_metadata`` (SQL column ``metadata``) — Portable
      :class:`JSON` → :class:`JSONB`, NOT NULL DEFAULT ``{}``.
      Forward-compat escape hatch the same shape
      :attr:`AuditLog.payload` uses. The Python attribute is
      ``doc_metadata`` because ``metadata`` is reserved on
      :class:`DeclarativeBase` (it's the table-registry attribute);
      :func:`mapped_column` carries the SQL column name explicitly
      so the table-side identifier matches the migration verbatim.
    * ``created_at`` / ``updated_at`` — ``timestamptz`` with PG
      server defaults of ``now()`` and ORM-side
      ``default=lambda: datetime.now(UTC)``. ``updated_at`` also
      sets ``onupdate=lambda: datetime.now(UTC)`` so ORM UPDATEs
      bump the timestamp; raw-SQL UPDATEs against PG do not fire
      this hook, which is acceptable in v0.2 because the
      substrate's only writer is the ORM-backed
      :func:`index_document` helper.

    Indexes:

    * ``documents_tenant_source_id_idx`` — unique composite btree on
      ``(tenant_id, source, source_id)``. The natural-key upsert
      target for :func:`index_document`. Uniqueness is enforced
      exclusively by this named index (no per-column ``unique=True``)
      so PG does not auto-create a redundant duplicate.
    * ``documents_body_hash_idx`` — btree on ``body_hash``.
      Cost-optimisation lookup for the unchanged-body short-circuit
      during refresh.
    * ``documents_body_fts_idx`` (PG only) — GIN over
      ``to_tsvector('english', body)``. Powers the BM25 half of
      hybrid retrieval. Declared in migration ``0003`` via raw SQL
      because Alembic has no clean API for expression-based GIN
      indexes; intentionally **not** in :attr:`__table_args__`
      because declaring it would force SQLite to attempt creation
      and fail.
    * ``documents_embedding_idx`` (PG only) — IVFFlat over
      ``embedding`` with ``vector_cosine_ops`` and ``lists = 100``.
      Powers the cosine half of hybrid retrieval. Same migration-
      only handling as the FTS index for the same reason. The
      ``lists = 100`` parameter targets ~10k-document corpora per
      pgvector's recommendation; v0.2.next may switch to HNSW once
      G4 ships corpus-recall numbers.

    The model deliberately ships with no helper methods — read /
    write paths live in :mod:`meho_backplane.retrieval` (landed by
    G0.4 sibling Tasks T2-T5). The ORM class is a pure data shape.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(_PORTABLE_VECTOR_384, nullable=False)
    # ``metadata`` is reserved on :class:`DeclarativeBase` (the
    # table-registry attribute). The Python attribute is
    # ``doc_metadata``; the SQL column name carried as the first
    # positional argument to :func:`mapped_column` keeps the
    # migration's column identifier (``metadata``) authoritative on
    # the table side.
    doc_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "documents_tenant_source_id_idx",
            "tenant_id",
            "source",
            "source_id",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "documents_body_hash_idx",
            "body_hash",
            postgresql_using="btree",
        ),
    )

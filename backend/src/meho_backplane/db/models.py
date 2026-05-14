# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SQLAlchemy 2.x ORM models for the backplane database.

The module exposes the declarative :class:`Base` that every model
inherits from, plus :class:`AuditLog` (v0.1), :class:`Tenant`
(v0.2 / G0.1), and :class:`Target` (v0.2 / G0.3). The metadata
object on :data:`Base.metadata` is what :mod:`meho_backplane.alembic.env`
imports as ``target_metadata`` so ``alembic revision --autogenerate``
can diff the model graph against the live schema.

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
* ``target_id`` — UUID, nullable. Added by migration ``0004``; the
  G0.3 CRUD layer writes the value when a request operates on a
  specific target. Generic requests (health, policy listing) leave
  it NULL. No FK to ``targets.id`` in v0.2 by the same soft-FK
  discipline established for ``tenant_id``.
* ``parent_audit_id`` — UUID, nullable. Added by migration ``0006``;
  the G0.6 dispatcher writes the parent composite operation's
  ``audit_log.id`` here when a composite handler issues a recursive
  ``dispatch_child(...)`` call. Top-level dispatches leave it NULL.
  Drives the recursive-CTE traversal at audit-replay time (G8.1 /
  G8.2). No FK to ``audit_log.id`` in v0.2 by the same soft-FK
  discipline established for ``tenant_id`` / ``target_id``.

Indexes on :class:`AuditLog`:

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
* ``audit_log_target_id_idx`` — b-tree on ``target_id`` so
  per-target audit queries hit the index. Added by migration ``0004``.
* ``audit_log_parent_audit_id_idx`` — b-tree on ``parent_audit_id``
  so the recursive-CTE traversal at audit-replay time hits the index.
  Added by migration ``0006``.

Schema decisions for :class:`Target`:

* ``id`` — UUID primary key. Same portable :class:`Uuid` shape.
* ``tenant_id`` — UUID NOT NULL. Every target belongs to exactly one
  tenant. No FK clause in v0.2 (same soft-FK discipline as
  ``audit_log.tenant_id``); the G0.3 CRUD layer enforces referential
  integrity at the application layer until a tightening migration
  adds the FK.
* ``name`` — Text NOT NULL. Human-readable handle within the tenant.
  Uniqueness enforced by the named ``targets_tenant_name_idx``
  (unique b-tree on ``(tenant_id, name)``).
* ``aliases`` — JSON/TEXT[], nullable. Secondary names for the target
  (DNS aliases, legacy hostnames). Stored as native ``TEXT[]`` on
  PostgreSQL (GIN-indexed for containment queries) and as a JSON
  array on SQLite (no native ARRAY type; GIN index skipped there).
  Portable via ``JSON().with_variant(PG_ARRAY(Text), "postgresql")``.
* ``product`` — Text NOT NULL. Product family identifier
  (e.g. ``kubernetes``, ``ssh``). Indexed with ``tenant_id`` via
  ``targets_tenant_product_idx`` for "list targets by product"
  queries.
* ``host`` — Text NOT NULL. Connection hostname or IP address.
* ``port`` — Integer, nullable. Defaults to the product's standard
  port at the connection layer; NULL means "use default".
* ``fqdn`` — Text, nullable. Fully-qualified domain name when it
  differs from ``host`` (e.g. service mesh names).
* ``secret_ref`` — Text, nullable. Vault path for credentials
  (populated by the G0.3 credential-binding layer, not T1).
* ``auth_model`` — Text NOT NULL DEFAULT ``'shared_service_account'``.
  How the agent authenticates to this target. Extensible via string
  values; the default covers the v0.2 SSA pattern.
* ``vpn_required`` — Boolean NOT NULL DEFAULT ``false``. Whether the
  agent must establish a VPN tunnel before connecting.
* ``extras`` — JSON NOT NULL DEFAULT ``{}``. JSONB on PostgreSQL
  (binary, GIN-friendly), generic JSON on SQLite. Escape hatch for
  per-product structured data without DDL changes.
* ``notes`` — Text, nullable. Free-form operator notes.
* ``created_at`` / ``updated_at`` — ``timestamptz`` NOT NULL.
  PG-side ``now()`` server default via the migration; the ORM also
  declares ``default=lambda: datetime.now(UTC)`` for SQLite dev/test.
  The CRUD layer is responsible for updating ``updated_at`` on every
  write; no trigger is installed in v0.2 (added by convention in the
  application layer).

Indexes on :class:`Target`:

* ``targets_tenant_name_idx`` — unique b-tree on ``(tenant_id, name)``
  — enforces the one-name-per-tenant invariant.
* ``targets_tenant_product_idx`` — b-tree on ``(tenant_id, product)``
  — drives the "list targets by product in tenant" query.
* ``targets_aliases_gin_idx`` — GIN on ``aliases`` (PostgreSQL only).
  Enables ``@>`` / ``&&`` array-containment queries for alias lookups.
  Declared on the model; the migration skips it on non-PG dialects.

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

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = [
    "AuditLog",
    "Base",
    "Document",
    "EndpointDescriptor",
    "OperationGroup",
    "Target",
    "Tenant",
]


#: Portable JSON column type — :class:`JSONB` on PostgreSQL (binary
#: JSON, indexable by ``@>`` / GIN), generic :class:`JSON` (text)
#: on every other dialect including the SQLite dev/test path.
_PORTABLE_JSON: TypeEngine[dict[str, object]] = JSON().with_variant(JSONB(), "postgresql")

#: Portable ARRAY(Text) column type — native ``TEXT[]`` on PostgreSQL
#: (supports GIN-indexed containment queries); JSON array on every other
#: dialect including the SQLite dev/test path (no native ARRAY type).
_PORTABLE_ARRAY: TypeEngine[list[str]] = JSON().with_variant(PG_ARRAY(Text), "postgresql")


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
    # Nullable on purpose — requests that don't operate on a specific
    # target (health checks, policy listings) leave this NULL. The G0.3
    # CRUD layer writes the value when a request targets a specific
    # endpoint. No FK to ``targets.id`` in v0.2; same soft-FK discipline
    # as ``tenant_id``. Added by migration ``0004``.
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # Nullable on purpose — top-level dispatches leave this NULL.
    # Populated by the G0.6 dispatcher (#396) when a composite handler
    # (``source_kind='composite'``) issues a recursive ``dispatch_child``
    # call: the child row's ``parent_audit_id`` points at the composite
    # parent's ``audit_log.id``. Drives the recursive-CTE traversal at
    # audit-replay time (G8.1 / G8.2). No FK to ``audit_log.id`` in
    # v0.2 -- same soft-FK discipline as ``tenant_id`` / ``target_id``.
    # Added by migration ``0006``.
    parent_audit_id: Mapped[uuid.UUID | None] = mapped_column(
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
        Index(
            "audit_log_target_id_idx",
            "target_id",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_parent_audit_id_idx",
            "parent_audit_id",
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


class Target(Base):
    """A registered endpoint that MEHO agents may connect to.

    Per-tenant registry of SSH hosts, Kubernetes API servers, and
    other endpoints the governance layer mediates access to. The
    G0.3 CRUD layer (Tasks T2+) provisions these rows; the G0.3
    policy layer (Tasks T3+) evaluates them against operator
    permissions before yielding connection coordinates to an agent.

    ``tenant_id`` is NOT NULL — every target belongs to exactly one
    tenant. No FK to ``tenant.id`` in v0.2 by the same soft-FK
    discipline as ``audit_log.tenant_id``; the application layer
    enforces referential integrity at insert time until a tightening
    migration adds the FK.

    ``name`` is unique within a tenant (enforced by the named
    ``targets_tenant_name_idx`` unique b-tree). Operators reference
    targets by name in CLI commands and policy rules; the UUID is
    the stable cross-system identifier.

    ``aliases`` stores secondary names (DNS aliases, legacy hostnames)
    as a native ``TEXT[]`` on PostgreSQL (GIN-indexed for containment
    queries) and as a JSON array on SQLite. NULL means no aliases.

    ``extras`` is the forward-compat escape hatch for per-product
    structured fields that don't yet have first-class columns. v0.2
    always writes ``{}``; later tasks can write product-specific dicts.
    """

    __tablename__ = "targets"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NOT NULL — every target belongs to exactly one tenant.
    # No FK clause in v0.2; application layer enforces integrity.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL; empty list means no aliases. TEXT[] on PG, JSON array on SQLite.
    # Using [] (never NULL) avoids NULL vs empty ambiguity and simplifies
    # = ANY(aliases) queries on PG.
    aliases: Mapped[list[str]] = mapped_column(
        _PORTABLE_ARRAY,
        nullable=False,
        default=list,
    )
    product: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fqdn: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_model: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="shared_service_account",
    )
    vpn_required: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=False,
    )
    extras: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Unique index matches migration op.create_index(..., unique=True).
        # Using Index rather than UniqueConstraint avoids Alembic autogenerate
        # drift (autogenerate sees constraint vs index as different objects).
        Index(
            "targets_tenant_name_idx",
            "tenant_id",
            "name",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "targets_tenant_product_idx",
            "tenant_id",
            "product",
            postgresql_using="btree",
        ),
        Index(
            "targets_aliases_gin_idx",
            "aliases",
            postgresql_using="gin",
        ),
        sa.CheckConstraint(
            "auth_model IN ('impersonation', 'shared_service_account', 'per_user')",
            name="ck_targets_auth_model",
        ),
    )


class OperationGroup(Base):
    """A named grouping of operations within one connector implementation.

    Initiative #388 (G0.6) substrate. Every row carries an
    LLM-summarised ``when_to_use`` blurb that the
    ``list_operation_groups`` meta-tool (T8 #399) returns verbatim so
    the agent can pick the right group before calling
    ``search_operations`` against it. Examples per connector:
    ``vmware-rest/9.0`` → ``vm-lifecycle`` / ``cluster`` / ``network``;
    ``vault/1.x`` → ``kv`` / ``sys`` / ``auth``.

    ``tenant_id`` is NULL for built-in / global groups (shipped by spec
    ingestion at G0.7 or by typed connectors at register-time) and
    populated for tenant-curated groups. No FK to ``tenant.id`` in
    v0.2 — same soft-FK discipline as ``audit_log.tenant_id``.

    Uniqueness is enforced by **two partial unique indexes** rather
    than a single composite UNIQUE because SQL's NULL != NULL
    semantics mean a single ``UNIQUE (tenant_id, product, version,
    impl_id, group_key)`` constraint would not catch duplicate
    built-in rows. Migration ``0005`` documents the pattern; the same
    split applies to :class:`EndpointDescriptor`.

    ``review_status`` is a bounded enum enforced via a DB-layer CHECK
    (``'staged'`` → freshly ingested, awaiting operator review;
    ``'enabled'`` → live for dispatch; ``'disabled'`` → hidden from
    retrieval). Portable enum-shape across PG + SQLite — see
    :class:`Target.auth_model` for the precedent.

    The model deliberately ships with no helper methods — population
    happens via T4's ``register_typed_operation()`` and G0.7's
    ingestion pipeline; queries land at the dispatcher and the
    meta-tools (T5 / T8).
    """

    __tablename__ = "operation_group"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NULL → built-in/global group; non-null → tenant-curated.
    # No FK clause in v0.2 by soft-FK discipline.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    product: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    impl_id: Mapped[str] = mapped_column(Text, nullable=False)
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    when_to_use: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="staged",
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
        # Partial unique on (product, version, impl_id, group_key) for
        # built-in/global rows. WHERE clause emitted by SQLAlchemy on
        # both dialects via the postgresql_where / sqlite_where pair.
        Index(
            "operation_group_global_idx",
            "product",
            "version",
            "impl_id",
            "group_key",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NULL"),
            sqlite_where=sa.text("tenant_id IS NULL"),
        ),
        # Partial unique on (tenant_id, product, version, impl_id,
        # group_key) for tenant-scoped rows.
        Index(
            "operation_group_tenant_idx",
            "tenant_id",
            "product",
            "version",
            "impl_id",
            "group_key",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "review_status IN ('staged', 'enabled', 'disabled')",
            name="ck_operation_group_review_status",
        ),
    )


class EndpointDescriptor(Base):
    """A single operation an agent can dispatch through the G0.6 substrate.

    Initiative #388 (G0.6) substrate. One row per
    (product, version, impl_id, op_id) — covers every operation the
    dispatcher (T5 #396) might route to, regardless of whether the
    operation was auto-derived from an OpenAPI spec (G0.7,
    ``source_kind='ingested'``), hand-coded into a typed connector
    (G3.x via T4 ``register_typed_operation()``,
    ``source_kind='typed'``), or authored as a composite
    (``source_kind='composite'`` with ``handler_ref`` pointing at a
    Python function that calls ``dispatch(...)`` recursively).

    ``op_id`` is the connector-side natural key. Examples:

    * Ingested HTTP — ``"GET:/api/vcenter/cluster"`` (method + path).
    * Typed — ``"vault.kv.read"`` / ``"k8s.pod.list"`` (dotted handle).
    * Composite — ``"vmware.composite.vm.create"`` (dotted handle).

    The ``method`` / ``path`` columns are populated for ingested rows
    and NULL for typed/composite; ``handler_ref`` (a Python dotted
    path) is populated for typed/composite and NULL for ingested. The
    dispatcher (T5) branches on ``source_kind`` to know which fields
    to consult.

    ``parameter_schema`` and ``response_schema`` are JSON Schema 2020-12
    documents (OpenAPI 3.1-compatible). The dispatcher validates
    inbound params against ``parameter_schema`` before routing; T4's
    helper and G0.7's ingestion populate both columns from the
    upstream source.

    ``safety_level`` + ``requires_approval`` are the policy-gate
    hooks. ``safety_level='safe'`` operations execute under the
    default-allow policy (v0.2); ``'caution'`` and ``'dangerous'``
    flow through G7 / G10 policy logic once those Goals land.
    ``requires_approval=true`` (independent of ``safety_level``)
    forces the dispatcher to write an audit row in
    ``status='pending'`` and wait for an operator decision before
    executing.

    ``llm_instructions`` carries per-op agent guidance (when to call,
    parameter collection hints, response interpretation tips). The
    field is JSON-shaped because consumers will want structured
    sub-fields (``"when_to_call"`` / ``"parameter_hints"`` /
    ``"output_format"``) without a schema change.

    ``embedding`` is :class:`pgvector.sqlalchemy.Vector(384)` on PG
    via the ``with_variant`` override, JSON-encoded :class:`Text` on
    SQLite via the shared :class:`_PortableVector384` decorator. Same
    dim (384) as :attr:`Document.embedding` — the agent's hybrid
    retrieval index (T8's ``search_operations``) shares the embedding
    pipeline G0.4 already set up. Nullable on both dialects because
    T1 ships the column shape only; T4 populates it before the row
    is dispatchable for retrieval.

    ``custom_description`` / ``custom_notes`` are operator-authored
    overrides applied at G0.7 ingest-review time. The ingestion
    pipeline writes ``description`` and ``summary`` verbatim from the
    upstream spec; the reviewer's customisation lives in
    ``custom_*`` so the original source-of-truth values stay
    auditable.

    Schema decisions for :class:`EndpointDescriptor`:

    * ``id`` — UUID primary key. Same portable :class:`Uuid` shape
      every other model uses.
    * ``tenant_id`` — UUID nullable. NULL → built-in/global op;
      non-null → tenant-scoped (composite owned by one tenant). No
      FK to ``tenant.id`` in v0.2 by soft-FK discipline.
    * ``group_id`` — UUID nullable with a real ``REFERENCES
      operation_group(id) ON DELETE SET NULL`` FK. Group-less
      descriptors stay dispatchable when their group is deleted; the
      operator's admin UI can re-group them later. See migration
      ``0005`` docstring for the cascade rationale.
    * Bounded enums (``source_kind``, ``safety_level``) — TEXT NOT
      NULL with DB-layer ``CHECK (column IN (...))`` constraints
      enforced by migration ``0005``. Same portable pattern
      :class:`Target.auth_model` uses.

    Indexes on :class:`EndpointDescriptor`:

    * Two partial unique indexes on
      ``(product, version, impl_id, op_id)`` — one ``WHERE
      tenant_id IS NULL`` for built-in rows, one ``WHERE
      tenant_id IS NOT NULL`` including ``tenant_id`` in the key
      for tenant-scoped rows. See :class:`OperationGroup` for the
      rationale on the partial-index split.
    * ``endpoint_descriptor_lookup_idx`` — b-tree on
      ``(product, version, impl_id, group_id, is_enabled)``. Drives
      "list every enabled op in group X for connector
      (product, version, impl_id)" queries from the dispatcher and
      the ``search_operations`` meta-tool.
    * ``endpoint_descriptor_bm25_idx`` (PG only) — GIN over
      ``to_tsvector('english', coalesce(summary, '') || ' ' ||
      coalesce(description, ''))``. Powers the BM25 half of
      ``search_operations``'s hybrid retrieval. Declared in
      migration ``0005`` via raw SQL because Alembic has no clean
      API for expression-based GIN; intentionally **not** in
      :attr:`__table_args__` because declaring it would force
      SQLite to attempt creation and fail.
    * ``endpoint_descriptor_embedding_idx`` (PG only) — IVFFlat over
      ``embedding`` with ``vector_cosine_ops`` and ``lists = 100``.
      Powers the cosine half of ``search_operations``'s hybrid
      retrieval. Same migration-only handling as the FTS index.
      The IVFFlat empty-table caveat applies (see migration
      docstring): ``REINDEX INDEX endpoint_descriptor_embedding_idx``
      after the first batch of operations is registered.

    The model deliberately ships with no helper methods — write
    paths are T4 (``register_typed_operation()``) and G0.7
    (ingestion), read paths are T5 (dispatcher) and T8 (meta-tools).
    """

    __tablename__ = "endpoint_descriptor"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    product: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    impl_id: Mapped[str] = mapped_column(Text, nullable=False)
    op_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    handler_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK with ON DELETE SET NULL — see model docstring.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("operation_group.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    tags: Mapped[list[str]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=list,
    )
    parameter_schema: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    response_schema: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    llm_instructions: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    safety_level: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="safe",
    )
    requires_approval: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=False,
    )
    is_enabled: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
    )
    # Nullable on both dialects — T1 ships the column shape only;
    # T4 populates it before the descriptor is dispatchable for
    # retrieval. Round-trips as list[float] via _PORTABLE_VECTOR_384
    # (JSON-encoded Text on SQLite, native vector(384) on PG).
    embedding: Mapped[list[float] | None] = mapped_column(
        _PORTABLE_VECTOR_384,
        nullable=True,
        default=None,
    )
    custom_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            "endpoint_descriptor_global_idx",
            "product",
            "version",
            "impl_id",
            "op_id",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NULL"),
            sqlite_where=sa.text("tenant_id IS NULL"),
        ),
        Index(
            "endpoint_descriptor_tenant_idx",
            "tenant_id",
            "product",
            "version",
            "impl_id",
            "op_id",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
        ),
        Index(
            "endpoint_descriptor_lookup_idx",
            "product",
            "version",
            "impl_id",
            "group_id",
            "is_enabled",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            "source_kind IN ('ingested', 'typed', 'composite')",
            name="ck_endpoint_descriptor_source_kind",
        ),
        sa.CheckConstraint(
            "safety_level IN ('safe', 'caution', 'dangerous')",
            name="ck_endpoint_descriptor_safety_level",
        ),
    )

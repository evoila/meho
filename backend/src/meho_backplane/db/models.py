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
* ``agent_session_id`` — UUID, nullable. Added by migration ``0014``;
  the MCP-session correlation id. Populated only on MCP audit rows,
  sourced from the inbound ``Mcp-Session-Id`` header (wired by
  G8.2-T2). Chassis HTTP-side audit rows are not agent sessions by
  design and leave it NULL; pre-G8.2 rows stay NULL too (no backfill).
  Drives the per-session audit-replay query (``meho audit replay
  <session-id>``, G8.2-T6). No FK in v0.2 by the same soft-FK
  discipline as ``tenant_id`` / ``target_id`` / ``parent_audit_id`` —
  the session id is an opaque transport-header correlation key, not a
  row identifier in this schema.

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
* ``audit_log_agent_session_id_idx`` — b-tree on ``agent_session_id``
  so the per-session ``WHERE agent_session_id = ?`` probe (the
  ``meho audit replay <session-id>`` query shape) hits the index.
  Added by migration ``0014``.

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
* ``verify_tls`` — Boolean NOT NULL DEFAULT ``true``. Whether connector
  dispatch verifies the target's TLS certificate chain. Default-secure;
  setting it ``false`` is the audited per-target opt-out for
  self-signed / internal-CA appliances (the dispatch wiring that
  consumes it lands in #1781). Added by migration ``0044`` with
  ``server_default=true`` so pre-#1780 rows backfill to the secure
  state.
* ``tls_ca_pin`` — Text, nullable. Per-target CA-trust pin: a PEM
  string carrying the CA / cert the connector must trust for this
  target while keeping ``CERT_REQUIRED`` + hostname verification ON
  (``ssl.SSLContext.load_verify_locations(cadata=...)``, the secure
  govc-thumbprint pattern). ``NULL`` (the default) means "no pin".
  The secure supersession of ``verify_tls=false`` and mutually
  exclusive with it (enforced at the API layer). Added by migration
  ``0045``; nullable, so the add-column is safe on a populated table
  with no backfill. The dispatch path that consumes it lands in #1784.
* ``tls_server_name`` — Text, nullable. Per-target TLS SNI /
  certificate-verification hostname, decoupled from ``host`` (the TCP
  connect address + wire ``Host:`` header). ``NULL`` (the default)
  means "derive the SNI / verify name from ``host`` as today". When
  set, dispatch keeps ``base_url=https://<host>`` (connect + ``Host`` =
  IP) and threads ``extensions={"sni_hostname": <name>}`` so the cert
  is verified against the override name -- letting an operator keep
  ``verify_tls=true`` + a cert-CN-pinned FQDN while sending
  ``Host: <IP>`` (the config a cert-CN-pinning appliance accepts).
  Orthogonal to ``verify_tls`` / ``tls_ca_pin`` (no mutual exclusion).
  Added by migration ``0050``; nullable, so the add-column is safe on a
  populated table. The dispatch path that consumes it lands in #2002.
* ``extras`` — JSON NOT NULL DEFAULT ``{}``. JSONB on PostgreSQL
  (binary, GIN-friendly), generic JSON on SQLite. Escape hatch for
  per-product structured data without DDL changes.
* ``notes`` — Text, nullable. Free-form operator notes.
* ``fingerprint`` — JSON nullable. Cached
  :class:`~meho_backplane.connectors.schemas.FingerprintResult` from
  the last successful probe (vendor / product / version / build /
  reachable / probed_at / probe_method / extras). ``NULL`` until first
  probe; populated by the probe route via
  :meth:`~meho_backplane.connectors.base.Connector.fingerprint`. The
  G0.6 resolver reads this column to pick a connector implementation
  without re-probing the live target. Added by migration ``0009``.
* ``preferred_impl_id`` — Text nullable. Operator override for the
  G0.6 resolver's tie-break ladder (#393): when multiple connector
  impls advertise overlapping ``(product, version)`` ranges, the
  resolver consults this column first and falls back to
  :attr:`~meho_backplane.connectors.base.Connector.priority` only when
  ``preferred_impl_id`` is ``NULL``. Plain text in v0.2 — same soft-FK
  discipline as ``product`` (matched against the in-process connector
  registry). Added by migration ``0009``.
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
from enum import StrEnum

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = [
    "EVENT_OUTBOX_NOTIFY_CHANNEL",
    "AgentRun",
    "AgentRunStatus",
    "AgentRunTrigger",
    "AuditLog",
    "Base",
    "BroadcastOverride",
    "BudgetWindowKind",
    "Document",
    "EndpointDescriptor",
    "EventOutbox",
    "GraphEdge",
    "GraphEdgeHistory",
    "GraphEdgeKind",
    "GraphHistoryChangeKind",
    "GraphNode",
    "GraphNodeHistory",
    "IdentityBudget",
    "OperationGroup",
    "RunbookRun",
    "RunbookRunStepState",
    "RunbookTemplate",
    "SpecProvenance",
    "Target",
    "Tenant",
    "TenantConvention",
    "TenantConventionHistory",
    "WebSession",
]


#: PostgreSQL ``LISTEN/NOTIFY`` channel name the event-outbox drain
#: loop subscribes to, and the same channel the writer ``NOTIFY``s on
#: after an outbox insert commits. The notification is a **latency
#: hint** only; the drain loop's durable guarantee comes from the
#: outbox table (G11.3-T3 #824). A dropped notification is benign --
#: the next polled tick picks the row up anyway. Channel names in PG
#: are quoted-lower-case identifiers; lowercase + underscore keeps
#: the ``LISTEN`` / ``NOTIFY`` statements quoting-free.
EVENT_OUTBOX_NOTIFY_CHANNEL: str = "event_outbox_new"


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
    # Nullable on purpose — the MCP-session correlation id. Populated
    # only on MCP audit rows, sourced from the inbound ``Mcp-Session-Id``
    # header (wired by G8.2-T2). Chassis HTTP-side audit rows are not
    # agent sessions by design and leave this NULL; pre-G8.2 rows stay
    # NULL too (no backfill). Drives the per-session audit-replay query
    # (``meho audit replay <session-id>``, G8.2-T6). No FK in v0.2 --
    # the session id is an opaque transport-header correlation key, not
    # a row identifier in this schema. Added by migration ``0014``.
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # RFC 8693 actor (``act.sub``) — the agent that acted on behalf of
    # ``operator_sub`` in a user-initiated agent run (G11.2-T2 #816). MEHO
    # synthesises the delegation binding at the resource server (Keycloak has
    # no delegation token exchange), binding the acting agent's principal into
    # the audit context for the run's lifetime. ``NULL`` for direct-user
    # requests and for autonomous (``client_credentials``) agent runs, where
    # the agent is the subject and there is no separate actor. Added by
    # migration ``0021``.
    actor_sub: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    # Connector-boundary redaction middleware (G11.4-T2 #1071). The
    # dispatcher captures the raw connector response, hands it to the
    # redaction engine, and stores the raw payload here verbatim so an
    # auditor can reconstruct the pre-redaction view (the trust boundary
    # is the API surface, not the audit log — internal incident response
    # needs the raw record). ``redaction_manifest`` carries one entry
    # per rule firing (``rule`` / ``pattern`` / ``action`` / ``count`` /
    # ``span`` / ``reason`` / ``path``) plus the resolved ``policy_id``;
    # the C1-d round-trip CI gate (#1073) replays the manifest against
    # the raw payload to confirm the redactor stays deterministic across
    # policy revisions. Both columns are nullable: pre-G11.4 audit rows
    # carry NULL, and error-path rows (handler/connector raised before
    # producing a response) have no raw payload to redact. Added by
    # migration ``0030``.
    raw_payload: Mapped[object] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    redaction_manifest: Mapped[object] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    # Runbook correlation (G12.1-T2 #1292). The dispatcher contextvar
    # populates ``run_id`` + ``step_id`` for every operation issued inside
    # a runbook run; pre-G12.1 rows and operations outside a run context
    # carry NULL on both columns. Soft-FK discipline — no DB-level FK
    # constraint to ``runbook_runs.run_id`` in v0.2, same as
    # ``parent_audit_id`` / ``target_id`` / ``agent_session_id``. Added by
    # migration ``0034``.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    step_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    # External change-ticket reference (work_ref I1-T1 #1655). Correlates
    # a governed MEHO operation to the out-of-band change record that
    # authorised it (a GitHub issue, a Jira ticket, a CR id) -- an opaque
    # string such as ``"gh:evoila/meho#1"`` carried on the same ContextVar
    # mechanism as ``run_id`` / ``agent_session_id`` / ``parent_audit_id``
    # (:data:`meho_backplane.operations._audit.work_ref_var`). NULL when no
    # work_ref is bound: the bind source is a separate task (I1-T2), so
    # today only a direct contextvar bind populates it, and the
    # system-internal writers (memory/topology/reaper/ui-session) leave it
    # NULL by design. No FK -- same soft-reference discipline as
    # ``tenant_id`` / ``target_id`` / ``parent_audit_id``. Added by
    # migration ``0039``.
    work_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    # Policy-gate verdict stamped on the row (#130). The synchronous gate
    # (:func:`meho_backplane.operations._validate.policy_gate`) computes a
    # :class:`PermissionVerdict` on every governed dispatch; the dispatcher
    # binds it to :data:`meho_backplane.operations._audit.policy_decision_var`
    # and both audit writers (the dispatch-row writer and the approval-queue
    # writer) read it here so a consumer can ``WHERE policy_decision = ...``
    # without joining ``method``+``path`` and parsing ``payload``. The closed
    # vocabulary is the real ``PermissionVerdict`` enum
    # (``auto-execute`` / ``needs-approval`` / ``deny``) — DB-enforced by the
    # CHECK below, matching ``agent_permission.verdict``. Nullable: pre-#130
    # rows, and rows written on a path where no gate ran (pre-gate usage
    # errors, system-internal writers), carry NULL. Added by migration
    # ``0051``. No index — it is a low-cardinality equality filter usually
    # combined with a time/principal predicate that an existing index covers.
    policy_decision: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )

    __table_args__ = (
        # Verdict vocabulary is the closed ``PermissionVerdict`` set. The
        # literal mirrors ``_PERMISSION_VERDICTS`` (defined later in this
        # module, after the enum) rather than referencing it — this
        # ``__table_args__`` is evaluated at class-definition time, before the
        # enum/helper exist. The enum is intentionally closed (a fourth verdict
        # is a coordinated code+migration change), so the duplication cannot
        # drift silently. ``... IS NULL`` keeps pre-#130 / no-gate rows valid.
        sa.CheckConstraint(
            "policy_decision IN ('auto-execute', 'needs-approval', 'deny') "
            "OR policy_decision IS NULL",
            name="ck_audit_log_policy_decision",
        ),
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
        Index(
            "audit_log_agent_session_id_idx",
            "agent_session_id",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_actor_sub_idx",
            "actor_sub",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_run_id_idx",
            "run_id",
            postgresql_using="btree",
        ),
        Index(
            "audit_log_work_ref_idx",
            "work_ref",
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
    # Operator-asserted product version, e.g. ``"9.0"``, ``"1.x"``.
    # Nullable so a fresh target without out-of-band version knowledge
    # can still be created and probed; once the probe succeeds the
    # authoritative ``fingerprint.version`` takes precedence at resolver
    # time (see
    # :func:`~meho_backplane.connectors.resolver._resolve_target_version`).
    # G0.15-T6 (#1215) ships this column to break the chicken-and-egg
    # the v0.7.0 dogfood surfaced (RDC #753, signal 6): every typed
    # connector except K8s required ``fingerprint.version`` to resolve,
    # but the probe needed the resolver to find a connector first. The
    # column is the operator-driven entry point; the wildcard
    # registrations fanned out to every typed connector in the same
    # PR are the always-resolvable fallback. Added by migration
    # ``0032``.
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # Whether connector dispatch verifies the target's TLS certificate
    # chain. Default-secure (``True``); ``False`` is the audited
    # per-target opt-out for self-signed / internal-CA appliances. This
    # column only *stores* the flag -- the dispatch path that reads it
    # (passing ``verify=<insecure SSLContext>`` to the pooled httpx
    # client) lands in #1781. ``server_default=true`` in migration
    # ``0044`` backfills pre-#1780 rows to the secure state so the
    # ``NOT NULL`` add-column is safe on a populated table.
    verify_tls: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
        server_default=sa.true(),
    )
    # Per-target CA-trust pin: a PEM string carrying the CA / cert the
    # connector must trust for *this* target, on top of the global
    # ``SSL_CERT_FILE`` bundle. ``NULL`` (the default) means "no pin --
    # verify against the global bundle only". When set, dispatch builds a
    # context with ``ssl.SSLContext.load_verify_locations(cadata=<pem>)``
    # which keeps ``CERT_REQUIRED`` + ``check_hostname`` ON (the secure
    # govc-thumbprint pattern, #1784), so the chain + hostname are still
    # enforced -- against the pinned CA. This is the secure supersession
    # of ``verify_tls=false``: the two are mutually exclusive (a pin makes
    # the insecure opt-out unnecessary), enforced at the API layer. Added
    # by migration ``0045``; nullable, so the add-column is safe on a
    # populated table with no backfill (existing rows read back ``NULL`` =
    # unpinned). The dispatch path that consumes it lives in
    # :mod:`meho_backplane.connectors.adapters.http`.
    tls_ca_pin: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-target TLS SNI / certificate-verification hostname, decoupled
    # from ``host`` (the TCP connect address + wire ``Host:`` header).
    # ``NULL`` (the default) means "derive the SNI / verify name from
    # ``host`` as today", so existing rows keep their behaviour
    # byte-identical. When set, dispatch keeps ``base_url=https://<host>``
    # (connect + ``Host`` = the IP a cert-CN-pinning appliance accepts)
    # and threads ``request(..., extensions={"sni_hostname": <name>})``
    # so httpcore drives ``server_hostname`` from the override -- the TLS
    # handshake offers it as SNI and verifies the presented cert's
    # CN/SAN against it. This lets an operator keep ``verify_tls=true``
    # (and optionally ``tls_ca_pin``) while routing ``Host: <IP>``,
    # instead of dropping to the insecure ``verify_tls=false`` to dodge a
    # hostname mismatch (#2002). Orthogonal to ``verify_tls`` /
    # ``tls_ca_pin`` -- it moves the verification *name*, not the trust
    # material or the verify on/off switch -- so there is no mutually
    # exclusive combination to reject. Added by migration ``0050``;
    # nullable, so the add-column is safe on a populated table with no
    # backfill. The dispatch path that consumes it lives in
    # :mod:`meho_backplane.connectors.adapters.http`.
    tls_server_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    extras: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cached :class:`FingerprintResult` from the last successful probe.
    # NULL until first probe; the probe route persists
    # ``FingerprintResult.model_dump(mode='json')`` here. Added by
    # migration ``0009``. JSONB on PG, generic JSON (text) on SQLite.
    fingerprint: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    # Operator override for the G0.6 resolver's tie-break ladder when
    # multiple connector impls advertise overlapping ``(product, version)``
    # ranges. Soft-FK to ``Connector.impl_id`` (no real FK in v0.2 — the
    # registry is in-process). Added by migration ``0009``.
    preferred_impl_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # Soft-delete timestamp. NULL → live row; non-NULL → wall-clock
    # time of the DELETE call. Written by ``DELETE /api/v1/targets/{name}``
    # (G0.14-T4 #1145); never rewritten. Every read path
    # (:func:`~meho_backplane.targets.resolver.resolve_target`, the
    # list endpoint, the dispatcher) filters
    # ``WHERE deleted_at IS NULL`` so a soft-deleted target is
    # invisible to the resolver while staying queryable from the
    # ``audit_log.target_id`` soft-FK (which would otherwise dangle).
    # Added by migration ``0028``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
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


class DocCollection(Base):
    """A named documentation corpus an agent can search (collections-as-data).

    Initiative #1548 (G4.6 Doc-collection catalogue), Task #1550 (T1)
    substrate. One row per corpus (e.g. ``vmware``). This table is the
    docs analogue of :class:`Target` — ``list_targets`` answers "what
    infra can I act on?", ``list_doc_collections`` (T4 #1553) answers
    "what docs can I search?". The registry is **authoritative for
    identity + backend binding** (operator-set); the liveness fields
    (``doc_count`` / ``last_ingested_at`` / ``readiness``) are
    probe-written from the backend (T6 #1555) — the same data split
    ``targets`` rows + ``Target.fingerprint`` use.

    ``tenant_id`` is NULL for global / shared collections (available to
    every tenant) and populated for tenant-curated collections, the
    NULLABLE-tenant idiom :class:`OperationGroup` established. No FK to
    ``tenant.id`` in v0.x by the same soft-FK discipline as
    ``audit_log.tenant_id``.

    Uniqueness on ``collection_key`` is enforced by **two partial unique
    indexes** rather than a single composite UNIQUE because SQL's
    NULL != NULL semantics mean a single ``UNIQUE (tenant_id,
    collection_key)`` would not catch two global rows sharing a
    ``collection_key``. The split lets a global ``vmware`` row and a
    tenant-curated ``vmware`` row coexist (the resolver prefers the
    tenant row); see :class:`OperationGroup` for the precedent.

    ``backend`` is the ``{type, ref}`` routing record the T2 (#1551)
    backend-agnostic search router resolves server-side — the backend
    (``vertex-rag`` / ``meho-knowledge``) never appears in a request or
    response. Operator-set; never probe-written.

    ``status`` is a bounded enum enforced via a DB-layer CHECK
    (``'provisioning'`` → registered, corpus not yet answerable;
    ``'ready'`` → live for search; ``'rebuilding'`` → a managed-RAG
    index rebuild is in flight; ``'disabled'`` → hidden from the
    catalogue). Portable enum-shape across PG + SQLite — see
    :attr:`OperationGroup.review_status` for the precedent. T3 (#1552)
    fails typed against a not-``ready`` collection; the ``readiness``
    JSON column lands here (NULL until T6's probe writes it) so that
    typed failure has somewhere to read its detail from.

    ``when_to_use`` mirrors :attr:`OperationGroup.when_to_use`: a blurb
    ``list_doc_collections`` returns verbatim so an agent can pick the
    right collection *before* searching.

    ``extras`` is the forward-compat escape hatch for per-collection
    structured fields without first-class columns; v1 writes ``{}``.

    The model ships with no helper methods — population is operator-
    managed seed for v1 (no create/import API until a collection needs
    one); read paths land at the resolver (this task) and the
    catalogue / search tools (T3 / T4).
    """

    __tablename__ = "doc_collections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NULL → global/shared collection (every tenant sees it); non-null →
    # tenant-curated. No FK clause by soft-FK discipline.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # Stable operator-chosen id, e.g. ``"vmware"``. The binary routing +
    # entitlement key the agent passes as ``collection=<key>`` (#1548).
    collection_key: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    # Products the corpus covers, e.g. ``["vsphere", "nsx"]``. TEXT[] on
    # PG (GIN-indexable containment), JSON array on SQLite. NOT NULL with
    # an empty-list default to avoid NULL vs [] ambiguity (Target.aliases
    # precedent).
    products: Mapped[list[str]] = mapped_column(
        _PORTABLE_ARRAY,
        nullable=False,
        default=list,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Agent-facing "pick this collection when…" blurb. Mirrors
    # OperationGroup.when_to_use; surfaced verbatim by list_doc_collections
    # (T4) and the initialize.instructions catalogue band.
    when_to_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator-set ``{type, ref}`` backend routing record (the T2 router
    # key). JSONB on PG, generic JSON (text) on SQLite. NOT NULL with no
    # default — every collection must bind to exactly one backend, so a
    # writer has to supply ``{type, ref}`` explicitly. Unlike
    # ``products`` / ``extras`` / ``readiness`` (where empty is a valid
    # state), an empty ``backend`` is a routing-broken row, so there is
    # no silent ``{}`` fallback at the ORM or migration layer.
    backend: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="provisioning",
    )
    # Probe-written liveness (T6 #1555). NULL until the first probe.
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    doc_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Probe-written readiness detail (T6 #1555); the column lands here so
    # T3 (#1552) can fail typed against a not-ready collection. NULL until
    # the first probe writes it. JSONB on PG, generic JSON on SQLite.
    readiness: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    extras: Mapped[dict[str, object]] = mapped_column(
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
        # Partial unique on (collection_key) for global rows. The WHERE
        # clause is emitted on both dialects via the postgresql_where /
        # sqlite_where pair (OperationGroup precedent).
        Index(
            "doc_collections_global_idx",
            "collection_key",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NULL"),
            sqlite_where=sa.text("tenant_id IS NULL"),
        ),
        # Partial unique on (tenant_id, collection_key) for tenant rows.
        Index(
            "doc_collections_tenant_idx",
            "tenant_id",
            "collection_key",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'rebuilding', 'disabled')",
            name="ck_doc_collections_status",
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


class SpecProvenance(Base):
    """One row per accepted spec ingest — durable, non-spoofable provenance.

    A single spec fans out to hundreds of :class:`EndpointDescriptor`
    rows, so provenance lives at the spec level rather than as per-row
    columns (#2291). Before this table the only per-row provenance was
    the spoofable ``spec:<uri>`` tag: an operator's hand-mutated inline
    upload labelled with a vendor's ``https`` URL persisted identically
    to a genuine fetch of that URL, so nothing downstream could tell a
    vendor artifact from a mutation.

    Columns:

    * ``uri`` — the audit label exactly as the operator presented it
      (``spec:`` / ``https://`` / ``file:///`` / ``docs:`` form
      preserved). It is *not* a trust signal on its own; ``origin`` +
      ``sha256`` are.
    * ``sha256`` — hex digest over the **raw spec bytes** (fetched body
      or uploaded content), computed at the ``_load_spec_bytes`` trust
      boundary before any YAML/JSON decode. Different content under the
      same ``uri`` changes this digest.
    * ``origin`` — how the bytes reached the backplane:
      ``fetched`` (https GET), ``inline`` (operator-uploaded content),
      or ``shipped`` (MEHO-authored catalog package data). This is the
      fetched-vs-inline bit that was never persisted before.
    * ``operator_sub`` — the ingesting operator's subject claim
      (nullable for boot-time shipped ingests with no operator).
    * ``ingested_at`` — UTC time the provenance row was last written;
      refreshed on re-ingest so it tracks the latest accepted ingest.

    Scope mirrors :class:`EndpointDescriptor`: ``tenant_id IS NULL`` is
    a built-in/global ingest, non-null is tenant-scoped. The natural key
    is ``(tenant_id, product, version, impl_id, uri)`` enforced by two
    partial unique indexes (NULL != NULL under SQL UNIQUE, so global and
    tenant rows need separate partial indexes — same shape the
    descriptor table uses). Re-ingesting the same spec under the same
    key updates the row in place (new ``sha256`` + ``ingested_at``)
    rather than accumulating duplicates.
    """

    __tablename__ = "spec_provenance"

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
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    operator_sub: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "spec_provenance_global_idx",
            "product",
            "version",
            "impl_id",
            "uri",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NULL"),
            sqlite_where=sa.text("tenant_id IS NULL"),
        ),
        Index(
            "spec_provenance_tenant_idx",
            "tenant_id",
            "product",
            "version",
            "impl_id",
            "uri",
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "origin IN ('fetched', 'inline', 'shipped')",
            name="ck_spec_provenance_origin",
        ),
    )


#: Closed enum of :attr:`GraphNode.kind` values. Mirrored verbatim in
#: migration ``0007``'s ``_NODE_KINDS`` constant; the two MUST stay in
#: lock-step or the DB-layer CHECK constraint will reject ORM-shaped
#: inserts. Widening the vocabulary (G9.2's curated extensions) is a
#: migration that updates both sides at once.
_GRAPH_NODE_KINDS: tuple[str, ...] = (
    "target",
    "vm",
    "host",
    "network",
    "datastore",
    "namespace",
    "pod",
    "service",
    "ingress",
    "node",
    "principal",
    "vault-role",
    "vault-mount",
    "volume",
)


class GraphEdgeKind(StrEnum):
    """Closed enum of :attr:`GraphEdge.kind` values -- v0.2 vocabulary.

    Initiative #364 (G9.2) locks the edge-kind vocabulary at ten members:
    the four auto-discoverable kinds G9.1 (#363) shipped, plus six
    operator-curated cross-system kinds that auto-discovery cannot infer
    (decision #6 in :file:`docs/planning/v0.2-decisions.md`). The vocabulary
    is closed -- widening it is a coordinated DB + model change (new
    migration, new enum member, new decision row) so the v0.2.next
    policy-engine grammar parsing ``kind`` stays portable across tenants.

    The four auto-discoverable kinds (refresh service writes these on
    every probe-derived edge):

    * :attr:`RUNS_ON` -- ``vm`` ``runs-on`` ``host``, ``pod`` ``runs-on``
      ``node``: the physical / scheduling host of a workload.
    * :attr:`MOUNTS` -- ``vm`` ``mounts`` ``datastore``, ``pod`` ``mounts``
      ``volume``: storage attachment.
    * :attr:`ROUTES_THROUGH` -- ``ingress`` ``routes-through`` ``service``,
      ``service`` ``routes-through`` ``pod``: network routing path.
    * :attr:`BELONGS_TO` -- ``pod`` ``belongs-to`` ``namespace``, ``vm``
      ``belongs-to`` ``host`` (logical group membership).

    The six curated-only kinds (operator-asserted via
    ``meho topology annotate``; cannot be derived from probes):

    * :attr:`AUTHENTICATES_VIA` -- principal -> identity-provider node
      (e.g. ``k8s-sa-foo`` -> ``vault-role-bar``). The canonical
      cross-system example.
    * :attr:`DEPENDS_ON` -- cross-system functional dependency (e.g.
      ``service-X`` -> ``database-Y`` where neither side knows about the
      other in its own probe output).
    * :attr:`REPLICATES_TO` -- operator-asserted replication relationship
      between two storage / database nodes.
    * :attr:`BACKED_UP_BY` -- operator-asserted backup relationship.
    * :attr:`ROUTES_VIA` -- operator-asserted network path through an
      intermediary (e.g., ``vm-A`` -> ``firewall-X`` -> ``vm-B`` when the
      probes only see point-to-point reachability).
    * :attr:`POLICY_BINDS` -- RBAC / policy attachment that crosses
      connector boundaries (e.g., ``kubernetes-namespace-prod`` ->
      ``vault-policy-prod-read``).

    Mirrors the closed-enum pattern :class:`AuthModel`
    (:mod:`meho_backplane.connectors.schemas`) sets: a Python
    :class:`enum.StrEnum` paired with a portable DB ``CHECK`` constraint,
    both moved in lock-step by one Alembic migration so the enum and the
    constraint cannot drift.
    """

    RUNS_ON = "runs-on"
    MOUNTS = "mounts"
    ROUTES_THROUGH = "routes-through"
    BELONGS_TO = "belongs-to"
    AUTHENTICATES_VIA = "authenticates-via"
    DEPENDS_ON = "depends-on"
    REPLICATES_TO = "replicates-to"
    BACKED_UP_BY = "backed-up-by"
    ROUTES_VIA = "routes-via"
    POLICY_BINDS = "policy-binds"


#: Closed enum of :attr:`GraphEdge.kind` -- the v0.2 ten-kind vocabulary.
#: Derived from :class:`GraphEdgeKind` so the enum and the CHECK constraint
#: cannot drift; the drift guard
#: :func:`tests.test_topology_schema.test_graph_edge_kinds_match_enum`
#: enforces the equality at unit-test time.
_GRAPH_EDGE_KINDS: tuple[str, ...] = tuple(k.value for k in GraphEdgeKind)

#: Closed enum of :attr:`GraphEdge.source` -- ``auto`` for
#: probe-derived edges (T3 refresh), ``curated`` reserved for the
#: operator-asserted edges G9.2 lands. v0.2 writes ``auto`` exclusively.
_GRAPH_EDGE_SOURCES: tuple[str, ...] = ("auto", "curated")


def _ck_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    return f"{column} IN ({', '.join(f"'{v}'" for v in values)})"


class GraphNode(Base):
    """A node in the per-tenant topology graph.

    Initiative #363 (G9.1) substrate, Task #448 (T1). Each row models
    one object an agent may need to reason about: a registered target,
    a VM, a host, a network, a datastore, a namespace, a pod, a
    service, an ingress, a node, a principal, a vault mount, a vault
    role, a volume. The closed enum (``kind``) is documented on the
    migration; widening it is a coordinated DB + model change.

    Schema decisions for :class:`GraphNode`:

    * ``id`` -- UUID primary key. Same portable :class:`Uuid` shape the
      rest of the model graph uses; PG production gets a
      ``gen_random_uuid()`` server default via migration ``0007``, the
      ORM falls back to ``default=uuid.uuid4`` on SQLite and for
      out-of-band inserts.
    * ``tenant_id`` -- UUID NOT NULL with a real
      ``REFERENCES tenant(id)`` FK. Unlike :attr:`AuditLog.tenant_id`
      (chassis-era rows with no real tenant to point at; FK deferred
      to v0.2.next backfill), :class:`GraphNode` is a brand-new
      substrate with no pre-existing rows. Enforcing the FK at the DB
      layer is the cheapest point to make the ownership invariant
      unbreakable: T3's refresh service cannot silently insert orphan
      rows for a typo'd / deleted / replayed tenant id. No
      ``ondelete`` clause -- tenant deletion is a major operation
      that must clear the tenant's graph first; the default
      ``NO ACTION`` blocks the cascade.
    * ``kind`` -- Text NOT NULL with a DB-layer
      ``CHECK kind IN (...)`` constraint enforced by migration
      ``0007`` (see :data:`_GRAPH_NODE_KINDS` for the v0.2 vocabulary).
    * ``name`` -- Text NOT NULL. Human-readable handle within the
      tenant + kind axis. Uniqueness is enforced by the named
      ``graph_node_tenant_kind_name_idx`` (unique b-tree on
      ``(tenant_id, kind, name)``).
    * ``target_id`` -- UUID NULL with a real
      ``REFERENCES targets(id) ON DELETE SET NULL`` FK. NULL when the
      node is not itself a registered target (an inner-graph VM, pod,
      datastore). ``SET NULL`` because removing a target should not
      cascade-delete the topology data the agent may still want to
      reason about; the node lives on as a non-target row.
    * ``properties`` -- portable JSON -> JSONB NOT NULL DEFAULT
      ``{}``. Per-node structured data the connector populates at
      discover time (e.g. a VM's power state, a pod's status phase);
      the column is the forward-compat escape hatch for shape
      evolution without DDL changes.
    * ``discovered_by`` -- Text NOT NULL. Connector product slug
      (``vmware``, ``kubernetes``, ``vault``, ...) when probe-derived,
      or ``curated`` for operator-asserted rows (G9.2). No CHECK
      constraint -- the value space is open-ended as new connectors
      land.
    * ``first_seen`` -- ``timestamptz`` NOT NULL. PG-side ``now()``
      server default via the migration; the ORM also declares
      ``default=lambda: datetime.now(UTC)`` for SQLite dev/test.
    * ``last_seen`` -- ``timestamptz`` NULL. Refresh writes a
      timestamp on every observation; the refresh service nulls it
      out once a node has been absent past the configured threshold
      (the spec's soft-delete signal -- the row stays queryable for
      G9.3 history replay but is filtered out of default queries).

    Indexes on :class:`GraphNode`:

    * ``graph_node_tenant_kind_name_idx`` -- unique b-tree on
      ``(tenant_id, kind, name)``. Enforces the "one (kind, name) per
      tenant" invariant at the DB layer. Named index only; no
      ``unique=True`` on the column triple to avoid PG auto-generating
      a duplicate anonymous index alongside it.

    The model deliberately ships with no helper methods -- write paths
    land in T3's refresh service, read paths in T4's recursive-CTE
    traversal helpers and the API/CLI/MCP fronts in T5--T7.
    """

    __tablename__ = "graph_node"

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
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("targets.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    properties: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    discovered_by: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "graph_node_tenant_kind_name_idx",
            "tenant_id",
            "kind",
            "name",
            unique=True,
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            _ck_in("kind", _GRAPH_NODE_KINDS),
            name="ck_graph_node_kind",
        ),
    )


class GraphEdge(Base):
    """A directed edge between two :class:`GraphNode` rows.

    Initiative #363 (G9.1) substrate, Task #448 (T1). Adjacency-list
    shape -- ``from_node_id`` and ``to_node_id`` are explicit columns
    -- so PG 16's ``WITH RECURSIVE ... CYCLE`` clause (§7.8.2.2 of
    the PG manual) can walk dependents (reverse) and dependencies
    (forward) without a graph extension.

    Schema decisions for :class:`GraphEdge`:

    * ``id`` -- UUID primary key. Same portable :class:`Uuid` shape.
    * ``tenant_id`` -- UUID NOT NULL with a real
      ``REFERENCES tenant(id)`` FK. Same rationale as
      :attr:`GraphNode.tenant_id`.
    * ``from_node_id`` / ``to_node_id`` -- UUID NOT NULL with real
      ``REFERENCES graph_node(id) ON DELETE CASCADE`` FKs.
      Hard-deleting a node hard-deletes its edges; refresh-driven
      soft-deletes (``GraphNode.last_seen=NULL``) leave the edges
      alone, so the cascade is invisible during normal operation and
      exists only for tenant purges + test cleanup.
    * ``kind`` -- Text NOT NULL with a DB-layer
      ``CHECK kind IN (...)`` constraint. The closed v0.2 ten-kind
      vocabulary is :class:`GraphEdgeKind` -- four auto-discoverable
      kinds (refresh writes these) plus six curated-only kinds
      (operator annotation only). :data:`_GRAPH_EDGE_KINDS` is derived
      from the enum so the CHECK constraint and the Python type cannot
      drift; widening requires a new Alembic migration that updates
      both in lock-step (G9.2 #364 / migration ``0010`` was the first
      widening, from G9.1's four to v0.2's ten).
    * ``source`` -- Text NOT NULL with a DB-layer
      ``CHECK source IN (...)`` constraint. ``auto`` for
      probe-derived (T3 refresh); ``curated`` for the
      operator-asserted edges G9.2 (#364) ships.
    * ``properties`` -- portable JSON -> JSONB NOT NULL DEFAULT
      ``{}``. Per-edge structured data (e.g. a mount's options, a
      route's port). Same forward-compat escape-hatch shape as
      :attr:`GraphNode.properties`.
    * ``discovered_by`` -- Text NOT NULL. Connector slug or
      ``curated``; same shape as :attr:`GraphNode.discovered_by`.
    * ``first_seen`` -- ``timestamptz`` NOT NULL. PG-side ``now()``
      server default; ORM falls back to
      ``default=lambda: datetime.now(UTC)`` on SQLite.
    * ``last_seen`` -- ``timestamptz`` NULL. Same soft-delete
      semantics as :attr:`GraphNode.last_seen` -- refresh writes a
      timestamp on observation, NULL signals soft-delete.

    Indexes on :class:`GraphEdge`:

    * ``graph_edge_tenant_endpoints_kind_idx`` -- unique b-tree on
      ``(tenant_id, from_node_id, to_node_id, kind)``. At most one
      edge of a given ``kind`` between a pair of nodes within a
      tenant.
    * ``graph_edge_tenant_from_idx`` -- b-tree on
      ``(tenant_id, from_node_id)``. Drives the *dependencies*
      (forward) recursive-CTE traversal in T4.
    * ``graph_edge_tenant_to_idx`` -- b-tree on
      ``(tenant_id, to_node_id)``. Drives the *dependents* (reverse)
      recursive-CTE traversal in T4.

    The model deliberately ships with no helper methods -- read /
    write paths live in the refresh service (T3) and the
    recursive-CTE traversal module (T4).
    """

    __tablename__ = "graph_edge"

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
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("graph_node.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("graph_node.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    properties: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    discovered_by: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "graph_edge_tenant_endpoints_kind_idx",
            "tenant_id",
            "from_node_id",
            "to_node_id",
            "kind",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "graph_edge_tenant_from_idx",
            "tenant_id",
            "from_node_id",
            postgresql_using="btree",
        ),
        Index(
            "graph_edge_tenant_to_idx",
            "tenant_id",
            "to_node_id",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            _ck_in("kind", _GRAPH_EDGE_KINDS),
            name="ck_graph_edge_kind",
        ),
        sa.CheckConstraint(
            _ck_in("source", _GRAPH_EDGE_SOURCES),
            name="ck_graph_edge_source",
        ),
    )


class BroadcastOverride(Base):
    """A per-tenant override rule for the G6.1 broadcast classifier.

    G6.3-T1 (Task #378) schema substrate under Initiative #376.
    Tenant admins write rows here to downgrade normally-full-detail
    operations to ``aggregate``-only on the SSE feed (and, via G6.2,
    the Slack mirror) -- the durable, scope-aware counterpart to the
    per-call ``X-Broadcast-Detail`` header that T3 will add.

    Resolution precedence (implemented in T2's
    :func:`compute_effective_broadcast_detail`):
    per-call request override > matching :class:`BroadcastOverride`
    row > the static :func:`classify_op` default in
    :mod:`meho_backplane.broadcast.events`. T1 ships only the table;
    the resolver and its per-tenant cache land in T2 (#379).

    Schema decisions:

    * ``id`` -- UUID primary key. Same portable :class:`Uuid` shape
      every other model uses; PG gets ``gen_random_uuid()`` via the
      migration, the ORM falls back to ``default=uuid.uuid4`` on
      SQLite.
    * ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES
      tenant(id)`` FK. Same precedent as :class:`Document`: a
      brand-new table with no chassis-era rows and a clean downgrade
      that drops the whole table can enforce the FK at the substrate
      boundary without a backfill/cascade trade-off. Orphan rows for
      a typo'd / deleted / replayed tenant id surface as
      :class:`IntegrityError` at insert time, not as unreachable
      override rows at resolver time.
    * ``op_id_pattern`` -- Text NOT NULL. Glob (``*`` plus literal);
      regex is deliberately rejected at the API layer (T4) -- see
      Initiative #376 "Out of scope". A glob like ``vault.kv.*`` or
      ``k8s.configmap.info`` (exact match) is the operator-facing
      shape; the resolver in T2 walks the per-tenant rule set in
      Python, so no DB-side glob index is needed in v0.2.
    * ``scope_field`` -- Text, nullable. ``NULL`` means an op-wide
      rule; non-null is one of a small allowlist (``"namespace"``
      for Kubernetes-shaped scope, ``"target_name"`` for
      vSphere/Vault-shaped scope). The allowlist is enforced at the
      Pydantic layer in T4 rather than as a DB ``CHECK`` constraint
      so future scope fields land without a migration. The
      :class:`Tenant` / :class:`EndpointDescriptor` / :class:`Target`
      precedents use DB-side ``CHECK`` for bounded enums; the
      forward-compat argument is what flips the decision here.
    * ``scope_value`` -- Text, nullable. The matching value for
      ``scope_field`` (e.g. ``"kube-system"``); ``NULL`` when
      ``scope_field`` is ``NULL``. The resolver treats the
      ``(scope_field, scope_value)`` pair atomically -- both NULL is
      "op-wide", both non-null is "scoped".
    * ``detail`` -- Text NOT NULL. ``"full"`` or ``"aggregate"`` --
      Pydantic ``Literal`` at the API layer (T4) is the enforcement
      point, mirroring the ``scope_field`` argument. The op-class
      label is **not** stored on the override row; the override
      shapes the publish-time detail decision and the static
      classifier still owns op-class assignment.
    * ``created_by_sub`` -- Text NOT NULL. JWT ``sub`` of the
      tenant-admin who wrote the rule -- captures who flipped a
      sensitivity floor for audit-trail / forensics. T4's CRUD verbs
      stamp this from the bound :class:`~meho_backplane.auth.operator.Operator`.
    * ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL.
      PG-side ``now()`` server defaults via the migration; the ORM
      also declares ``default=lambda: datetime.now(UTC)`` plus
      ``onupdate=lambda: datetime.now(UTC)`` on ``updated_at`` so
      ORM-side row edits (T4 ``PATCH``) bump the timestamp. Raw-SQL
      UPDATEs against PG do **not** fire the ORM hook, which is
      acceptable in v0.2 because the substrate's only writer is the
      ORM-backed T4 layer.

    Composite uniqueness on
    ``(tenant_id, op_id_pattern, scope_field, scope_value)`` is
    enforced by the named ``broadcast_override_tenant_unique_idx``
    -- a tenant admin who races two CRUD calls against the same
    ``(pattern, scope)`` triple lands the second insert as an
    :class:`IntegrityError` rather than as a duplicate-rule shadow
    that the resolver would have to disambiguate. The named index
    pattern matches the :class:`Tenant` / :class:`Target` /
    :class:`Document` precedent (single source of uniqueness, no
    PG-side duplicate from ``unique=True`` on the column). SQL's
    ``NULL != NULL`` semantics technically let two rows with
    ``(NULL, NULL)`` for the scope pair coexist under a vanilla
    composite UNIQUE; that is acceptable here because the op-wide
    rule is fully described by ``(tenant_id, op_id_pattern)`` and
    a duplicate ``(NULL, NULL)`` differs from a duplicate
    ``(\"namespace\", \"kube-system\")`` only in resolver
    ambiguity, which T2's resolver handles via deterministic
    tie-break (first-match by ``created_at`` order). A partial-
    index split like :class:`OperationGroup` uses is therefore not
    warranted in v0.2; if duplicate op-wide rules become an
    operational problem the tightening can ship in v0.2.next.

    Indexes on :class:`BroadcastOverride`:

    * ``broadcast_override_tenant_unique_idx`` -- unique composite
      b-tree on ``(tenant_id, op_id_pattern, scope_field,
      scope_value)``. The natural-key target for T4's upsert; the
      composite shape pins the per-tenant rule set down to a single
      row.
    * ``broadcast_override_tenant_idx`` -- b-tree on ``tenant_id``.
      Drives the resolver's tenant-scoped rule pull at publish time
      (T2's per-tenant cache hydrates from
      ``SELECT * FROM broadcast_override WHERE tenant_id = :id``).

    The model deliberately ships with no helper methods -- read /
    write paths land in T2 (the resolver + per-tenant cache) and T4
    (the CRUD verbs). The ORM class is a pure data shape.
    """

    __tablename__ = "broadcast_override"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NOT NULL with a real REFERENCES tenant(id) FK -- see class
    # docstring for the Document-precedent rationale. Orphan-row
    # insertion (typo / deleted / replayed contextvar) becomes an
    # IntegrityError at insert time rather than a silently dangling
    # override row at resolver time.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    op_id_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL -> op-wide rule; non-null is enforced against a small
    # allowlist by the Pydantic layer in T4 (no DB CHECK so future
    # scope fields land without a migration).
    scope_field: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "full" | "aggregate". Pydantic Literal at the API layer (T4).
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
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
            "broadcast_override_tenant_unique_idx",
            "tenant_id",
            "op_id_pattern",
            "scope_field",
            "scope_value",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "broadcast_override_tenant_idx",
            "tenant_id",
            postgresql_using="btree",
        ),
    )


class GraphHistoryChangeKind(StrEnum):
    """Closed enum of :attr:`GraphNodeHistory.change_kind` /
    :attr:`GraphEdgeHistory.change_kind` values.

    Initiative #365 (G9.3) locks the temporal-change vocabulary at three
    members -- one per discoverable mutation shape against the live
    :class:`GraphNode` / :class:`GraphEdge` tables. The diff-on-write
    hook (T2 #857) chooses one of these three values per history-row
    emission; the closed enum + DB-layer ``CHECK`` constraint move in
    lock-step via migration ``0012``, mirroring the discipline
    :class:`GraphEdgeKind` follows (migration ``0010``).

    The vocabulary is intentionally **not** the union of every possible
    mutation -- a noisy ``soft_deleted`` / ``rediscovered`` /
    ``last_seen_advanced`` split would push the diff-on-write hook
    toward expressing field-level deltas in the row shape, when the
    structured ``snapshot`` JSONB already carries that information.
    Three coarse change kinds keep the history row shape stable across
    refresh / annotate / unannotate paths.

    Members:

    * :attr:`CREATED` -- the row did not exist before this mutation;
      ``snapshot.before`` is NULL-shaped, ``snapshot.after`` carries the
      full post-insert row JSON.
    * :attr:`UPDATED` -- the row existed and one or more columns
      changed in place; ``snapshot.before`` carries the pre-mutation
      row JSON, ``snapshot.after`` the post-mutation row JSON.
    * :attr:`REMOVED` -- the row was hard-deleted or soft-deleted
      (``last_seen`` reset to NULL by the refresh service);
      ``snapshot.before`` carries the final row JSON, ``snapshot.after``
      is NULL-shaped. Drives the partial tombstone-replay index on
      both history tables.
    """

    CREATED = "created"
    UPDATED = "updated"
    REMOVED = "removed"


#: Closed enum of ``change_kind`` values used by both history tables.
#: Derived from :class:`GraphHistoryChangeKind` so the enum and the
#: DB-layer ``CHECK`` constraint cannot drift; the drift guard at
#: :mod:`tests.test_topology_history_migration` enforces the equality
#: at unit-test time. Mirrors the :data:`_GRAPH_EDGE_KINDS` pattern
#: :class:`GraphEdge` uses.
_GRAPH_HISTORY_CHANGE_KINDS: tuple[str, ...] = tuple(k.value for k in GraphHistoryChangeKind)


#: Dialect-portable ``BIGSERIAL`` substitute -- :class:`BigInteger` on
#: PostgreSQL (compiles to ``BIGSERIAL`` when paired with
#: ``primary_key=True`` + ``autoincrement=True``), :class:`Integer` on
#: SQLite (``INTEGER PRIMARY KEY`` is the rowid alias, the only shape
#: SQLite auto-increments; ``BIGINT PRIMARY KEY`` would not). The
#: ``with_variant`` swap keeps the Python contract a 64-bit signed
#: integer on PG and the actual width SQLite can rowid-alias on the
#: dev/test path. Used by both history tables for ``history_id``.
_PORTABLE_BIG_SERIAL: TypeEngine[int] = BigInteger().with_variant(Integer(), "sqlite")


class GraphNodeHistory(Base):
    """An append-only history row for one :class:`GraphNode` mutation.

    Initiative #365 (G9.3) substrate, Task #856 (T1). Mirrors the
    :class:`AuditLog` append-only recipe (one row per
    discovery-driven mutation, indexed by tenant + time, JSONB
    snapshot) and is the storage half of the diff-on-write hook T2
    (#857) lands. T1 ships only the table + the ORM model; no write
    path exists yet.

    Append-only semantics: the application never issues an UPDATE or
    DELETE against this table. ``removed`` rows are tombstones, not
    deletions -- the row stays for the operator-visible "when was
    this node removed?" query. Retention is bounded by the prune
    task T6 (#858) at ``TOPOLOGY_HISTORY_RETENTION_DAYS`` (default
    90); rows older than the retention window are dropped in one
    audited batch per run.

    Schema decisions for :class:`GraphNodeHistory`:

    * ``history_id`` -- ``BIGSERIAL`` on PG, autoincrementing
      ``INTEGER`` on SQLite. Insert-ordered, monotonic, cheap. The
      append-only shape makes a 64-bit counter the right primary key
      -- UUIDs would force the diff-on-write hook to either generate
      one Python-side (extra entropy on a write-heavy path) or read
      back the server-default (extra round-trip). See migration
      ``0012`` docstring for the dialect-portability rationale.
    * ``node_id`` -- ``UUID`` with a real
      ``REFERENCES graph_node(id) ON DELETE SET NULL`` FK. Nullable in
      the ORM signature so the SET NULL transition compiles; the diff-
      on-write hook always populates it. ``SET NULL`` rather than
      ``CASCADE`` is the load-bearing decision: history rows must
      survive the deletion of the live node they reference. A hard
      cascade would drop the entire history of a removed node -- the
      data G9.3 exists to preserve.
    * ``tenant_id`` -- ``UUID`` NOT NULL with a real
      ``REFERENCES tenant(id)`` FK. Same brand-new-substrate rationale
      as :class:`GraphNode` / :class:`GraphEdge` / :class:`Document` /
      :class:`BroadcastOverride`.
    * ``change_kind`` -- ``TEXT`` NOT NULL with a DB-layer
      ``CHECK change_kind IN ('created', 'updated', 'removed')``
      constraint. Mirrored in :class:`GraphHistoryChangeKind`.
    * ``snapshot`` -- portable JSON -> JSONB NOT NULL DEFAULT ``{}``.
      ``{before, after}`` projection; semantics per change-kind member
      docstrings on :class:`GraphHistoryChangeKind`.
    * ``audit_id`` -- ``UUID`` nullable. Soft-FK to
      :class:`AuditLog.id` -- the request whose contextvar carried
      the operation that caused this mutation. Same soft-FK discipline
      as :attr:`AuditLog.tenant_id` / :attr:`AuditLog.target_id` /
      :attr:`AuditLog.parent_audit_id` -- see migration ``0012``
      docstring for the retention-coupling rationale.
    * ``valid_from`` -- ``timestamptz`` NOT NULL. PG-side ``now()``
      server default; the ORM also declares
      ``default=lambda: datetime.now(UTC)`` for SQLite dev/test.

    Indexes on :class:`GraphNodeHistory`:

    * ``graph_node_history_tenant_node_valid_from_idx`` -- composite
      b-tree on ``(tenant_id, node_id, valid_from DESC)``. Drives the
      per-resource history walk (T3 ``meho topology history``).
    * ``graph_node_history_tenant_valid_from_idx`` -- composite b-tree
      on ``(tenant_id, valid_from DESC)``. Drives the tenant-wide
      timeline scan (T5 ``meho topology timeline``).
    * ``graph_node_history_tenant_removed_idx`` -- **partial** b-tree
      on ``(tenant_id, valid_from DESC) WHERE change_kind = 'removed'``.
      Drives the tombstone-replay query. The partial keeps only the
      tombstone rows (typically << 5% of table volume on a healthy
      refresh cadence) so the query is a single indexed scan rather
      than a full timeline scan + post-filter.

    The model deliberately ships with no helper methods -- write paths
    land in T2's diff-on-write hook, read paths in T3 / T4 / T5's
    temporal-query verbs. Indexes are declared at the **migration**
    level (with DESC ordering on ``valid_from``) rather than via
    ``__table_args__`` because ``Index(... sa.text("col DESC") ...)``
    does not round-trip through Alembic's autogenerate detection; the
    migration is the single source of truth.
    """

    __tablename__ = "graph_node_history"

    history_id: Mapped[int] = mapped_column(
        _PORTABLE_BIG_SERIAL,
        primary_key=True,
        autoincrement=True,
        nullable=False,
    )
    # Nullable so the ON DELETE SET NULL transition compiles. The
    # diff-on-write hook in T2 always populates it on insert; the NULL
    # state only appears after the referenced node is hard-deleted.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("graph_node.id", ondelete="SET NULL"),
        nullable=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    change_kind: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    # Soft-FK to audit_log.id -- see class / migration 0012 docstring.
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # CHECK constraint mirrors :class:`GraphHistoryChangeKind`. Indexes
    # are declared at the migration layer (not here) because their DESC
    # ordering on ``valid_from`` does not round-trip through Alembic's
    # autogenerate path; the migration is the single source of truth
    # for index DDL.
    __table_args__ = (
        sa.CheckConstraint(
            _ck_in("change_kind", _GRAPH_HISTORY_CHANGE_KINDS),
            name="ck_graph_node_history_change_kind",
        ),
    )


class GraphEdgeHistory(Base):
    """An append-only history row for one :class:`GraphEdge` mutation.

    Initiative #365 (G9.3) substrate, Task #856 (T1). Mirror of
    :class:`GraphNodeHistory` for the edge side of the topology graph;
    every refresh-driven or operator-annotated edge mutation produces
    one row in this table inside the same transaction as the live-row
    mutation (T2 #857).

    Schema decisions match :class:`GraphNodeHistory` exactly, with one
    column substitution: ``node_id`` becomes ``edge_id`` and points at
    :class:`GraphEdge.id` instead of :class:`GraphNode.id`. The
    ``ON DELETE SET NULL`` semantics, soft-FK to :class:`AuditLog.id`,
    closed-enum ``change_kind`` CHECK constraint, JSONB snapshot, and
    ``valid_from`` server defaults are all identical -- the two history
    tables are symmetric by design so the temporal-query verbs
    (T3 / T4 / T5) can compose against both with one query shape.

    Indexes on :class:`GraphEdgeHistory`:

    * ``graph_edge_history_tenant_edge_valid_from_idx`` -- composite
      b-tree on ``(tenant_id, edge_id, valid_from DESC)``. Drives the
      per-resource history walk for an edge (T3
      ``meho topology history --include-edges``).
    * ``graph_edge_history_tenant_valid_from_idx`` -- composite b-tree
      on ``(tenant_id, valid_from DESC)``. Drives the tenant-wide
      timeline scan (T5).
    * ``graph_edge_history_tenant_removed_idx`` -- **partial** b-tree
      on ``(tenant_id, valid_from DESC) WHERE change_kind = 'removed'``.
      Drives the edge-side tombstone-replay query.

    Same migration-layer-owned-DDL discipline as
    :class:`GraphNodeHistory`: indexes are declared in migration
    ``0012`` with explicit DESC ordering on ``valid_from``, not via
    ``__table_args__``.
    """

    __tablename__ = "graph_edge_history"

    history_id: Mapped[int] = mapped_column(
        _PORTABLE_BIG_SERIAL,
        primary_key=True,
        autoincrement=True,
        nullable=False,
    )
    # Nullable so the ON DELETE SET NULL transition compiles -- same
    # rationale as :attr:`GraphNodeHistory.node_id`.
    edge_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("graph_edge.id", ondelete="SET NULL"),
        nullable=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    change_kind: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        sa.CheckConstraint(
            _ck_in("change_kind", _GRAPH_HISTORY_CHANGE_KINDS),
            name="ck_graph_edge_history_change_kind",
        ),
    )


class WebSession(Base):
    """One row per active BFF (Backend-for-Frontend) operator session.

    Initiative #337 (G10.0 Frontend chassis), Task #864 (T3). The
    operator-console is locked to the BFF custody shape per decision
    #11 (``docs/planning/v0.2-decisions.md``): the browser holds an
    opaque session-cookie value (the row's ``id``), the real OAuth
    access + refresh tokens live encrypted in this row, and every
    authenticated ``/ui/*`` request resolves operator identity by
    looking up the row and decrypting the tokens server-side.

    This model is **storage-only** -- the encryption / rotation /
    replay-detection contract lives in
    :mod:`meho_backplane.ui.auth.session_store`. No helper methods on
    the model itself; the discipline ``AuditLog`` already follows
    (write-once + helper logic at the call site).

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. The cookie value the browser holds
      is the canonical 36-char form (``str(uuid)``). PG production
      gets ``gen_random_uuid()`` via migration ``0013``;
      :func:`uuid.uuid4` (CSPRNG-backed) is the ORM default for the
      SQLite dev/test path. ~122 bits of entropy makes session-id
      guessing computationally infeasible.

    * ``operator_sub`` -- Keycloak ``sub`` claim of the logged-in
      operator. Mirrors :attr:`AuditLog.operator_sub`; the chassis
      has no ``operator`` table, so the JWT ``sub`` is the operator's
      stable identifier end-to-end.

    * ``tenant_id`` -- The operator's active tenant at session-creation
      time, sourced from the JWT ``tenant_id`` claim. Soft-FK
      discipline: no FK to ``tenant.id``, matching the audit-log
      pattern (``audit_log.tenant_id``). Tenant deletion is a major
      ops operation that scrubs dependent sessions explicitly before
      removing the tenant row.

    * ``access_token`` / ``refresh_token`` -- Fernet-encrypted bytes.
      ``LargeBinary`` -> ``bytea`` on PG, ``BLOB`` on SQLite. The
      plaintext never lands in this column: every write passes
      through :mod:`meho_backplane.ui.auth.session_store` which
      :class:`cryptography.fernet.Fernet`-encrypts before insert and
      decrypts on read using the chassis-wide key resolved from
      :attr:`Settings.ui_session_encryption_key`. Storing bytes (not
      the URL-safe base64 string Fernet emits) avoids text-search
      tooling (``psql \\d``, future grep-the-audit-export flows)
      ever surfacing what looks like an OAuth token in stable
      storage.

    * ``created_at`` / ``expires_at`` -- timestamptz. ``created_at``
      gets a PG ``now()`` server default + ORM
      ``datetime.now(UTC)`` for SQLite; ``expires_at`` is supplied
      by the session-creation caller (#865) from the access-token's
      ``exp`` claim. :func:`load_session` filters on
      ``expires_at > now()``.

    * ``last_seen_at`` -- timestamptz, refreshed on every successful
      :func:`load_session` call. Drives future idle-revocation
      sweeps; never accepts a client-controlled value.

    * ``revoked_at`` -- timestamptz, NULL means active.
      Soft-delete shape: a revoked session row stays queryable for
      forensics and so the audit row written on refresh-token
      replay (which references the session id) remains back-
      traceable. The read-side filter in :func:`load_session` is
      ``revoked_at IS NULL AND expires_at > now()``.

    Indexes
    -------

    * ``web_session_operator_sub_idx`` -- btree on ``operator_sub``,
      drives the future "list / revoke all sessions for operator X"
      surface.
    * ``web_session_expires_at_idx`` -- btree on ``expires_at``,
      drives the future background sweep of naturally-expired
      sessions. The hot-path ``load_session`` query is a PK probe
      and does not need this index.
    """

    __tablename__ = "web_session"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    operator_sub: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Fernet ciphertext (bytes). Never plaintext -- the session_store
    # module is the only seam that reads or writes these columns.
    access_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    # NULL = active; non-NULL = revoked (logout, replay, op-revoke).
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "web_session_operator_sub_idx",
            "operator_sub",
            postgresql_using="btree",
        ),
        Index(
            "web_session_expires_at_idx",
            "expires_at",
            postgresql_using="btree",
        ),
    )


class TenantConvention(Base):
    """A tenant-scoped operational / workflow / reference rule.

    Initiative #229 (G7.1 Tenant conventions + Layer 2 starter), Task
    #313 (T1). Schema foundation only -- T2 (#314) lands the API
    routes, T3 (#315) the CLI verbs, T4 (#316) the session-preamble
    assembler that reads the ``kind='operational'`` subset ordered by
    ``priority DESC, created_at ASC``, T5 (#317) seeds rows for the
    ``rdc-internal`` tenant.

    Every row carries a per-tenant ``slug`` (operator-visible
    identifier; ``rbac-canonical``, ``secret-handling``, etc.), a
    ``title`` (display label), a free-form Markdown ``body``, a
    ``kind`` discriminator (``operational`` | ``workflow`` |
    ``reference``, enforced at the Pydantic layer in T2 -- not at the
    DB layer per the issue's Out of scope), and a SMALLINT
    ``priority`` (T4's preamble-packing ranking key, ``DEFAULT 0``).
    The ``(tenant_id, slug)`` pair is unique within the table -- two
    tenants can declare the same slug independently, but one tenant
    cannot have two conventions with the same slug.

    Soft-FK discipline
    ------------------

    ``tenant_id`` is NOT NULL with no ``REFERENCES tenant(id)``
    clause per the issue body's explicit choice (#229 body: "Soft FKs
    everywhere matches the chassis convention -- column types match
    the referenced tables but no REFERENCES ... clauses. Simplifies
    migration reversibility; v0.2.next can tighten."). The application
    layer (T2's CRUD) enforces referential integrity at insert time
    until a v0.2.next tightening migration adds the FK clauses.

    Why ``priority`` is :class:`SmallInteger`
    -----------------------------------------

    T4's preamble assembler packs operational conventions
    **highest-priority-first** and drops lowest-priority entries
    whole when over the token budget (never mid-entry truncation of
    an operational rule). The column is fundamentally a ranking key,
    not a real-number similarity score, so SMALLINT (-32768..32767)
    is more than enough range -- mirrors MCP 2025-06-18's own
    resource ``priority`` annotation semantic, on the integer column
    instead of the floating-point annotation, to avoid wasting a
    real-number comparison on what is fundamentally an ordering
    decision. ``NOT NULL DEFAULT 0`` so T2's ``ConventionCreate``
    contract stays backward-compatible (priority optional).

    Indexes
    -------

    * ``tenant_conventions_tenant_slug_idx`` -- unique composite btree
      on ``(tenant_id, slug)``. Single source of uniqueness enforcement
      and the natural-key probe for T2's ``GET /{slug}`` / ``PATCH /{slug}``
      / ``DELETE /{slug}`` routes. Same single-named-index discipline
      :class:`Tenant.slug` follows -- we deliberately omit ``unique=True``
      on the ``slug`` column so PG does not auto-create a redundant
      duplicate index alongside the named one.

    The model deliberately ships with no helper methods; convention
    rows are CRUD-shaped (read-mostly via the preamble assembler,
    write-rarely via the API/CLI) and the query patterns are simple
    enough to live at the call site in T2's CRUD module.
    """

    __tablename__ = "tenant_conventions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NOT NULL -- every convention belongs to exactly one tenant.
    # No FK clause in v0.2 per the issue body (soft-FK discipline);
    # the application layer enforces referential integrity at insert
    # time until a v0.2.next tightening migration adds the FK.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        nullable=False,
    )
    # Uniqueness enforced by the named composite
    # ``tenant_conventions_tenant_slug_idx`` below (declared
    # ``unique=True``). The column itself omits ``unique=True`` -- PG
    # would otherwise auto-create a second unique index alongside the
    # named one for zero benefit (same discipline as
    # :class:`Tenant.slug`).
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form text. Pydantic at the API layer (T2) bounds it to
    # ``operational`` | ``workflow`` | ``reference``; DB-level enum
    # deferred per the issue's Out of scope (Pydantic + application
    # validation is enough).
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # ``priority`` -- T4's preamble-packing ranking key. SMALLINT
    # because the value is fundamentally an ordering key, not a
    # real-number score. NOT NULL DEFAULT 0 so T2's
    # ``ConventionCreate`` contract stays backward-compatible
    # (priority optional, defaults to 0).
    priority: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
    )
    # ``created_by_sub`` -- JWT ``sub`` of the convention's creator.
    # Nullable for migration-seeded rows (T5's seed migration has no
    # operator context); T2's POST route populates it from the
    # authenticated principal.
    created_by_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            "tenant_conventions_tenant_slug_idx",
            "tenant_id",
            "slug",
            unique=True,
            postgresql_using="btree",
        ),
    )


class TenantConventionHistory(Base):
    """One row per edit to a :class:`TenantConvention`.

    Initiative #229, Task #313. Companion table to
    :class:`TenantConvention`; T2's CRUD routes insert a history row
    in the same DB transaction as every convention write (CREATE,
    UPDATE, DELETE) so the diff trail stays causally consistent with
    the current state. T3's ``meho conventions history <slug>`` verb
    reads from this table chronologically and cross-references the
    audit log via the ``audit_id`` soft-FK.

    The ``body_before`` column is nullable -- the first history row
    (the CREATE event) has no prior state. Subsequent PATCHes shift
    the previous body into ``body_before`` and the new body into
    ``body_after``. DELETE events get ``body_after=<final body>`` (a
    legible last-known state for audit forensics) rather than a
    sentinel marker; the lifecycle distinction lives in the audit
    row, not in this table.

    Soft-FK discipline
    ------------------

    Both ``convention_id`` and ``audit_id`` are soft FKs (column
    types match the referenced tables, no ``REFERENCES`` clause) per
    the issue body's explicit choice. ``convention_id`` is NOT NULL
    because a history row without a parent convention has no semantic
    meaning; ``audit_id`` is nullable to allow migration-seeded rows
    (T5's seed migration has no audit_log row to point at). T2's
    CRUD writes pull ``audit_id`` from the audit middleware's
    contextvar so G8's audit-query path can join history back to the
    originating request.

    Indexes
    -------

    * ``tenant_convention_history_convention_idx`` -- composite btree
      on ``(convention_id, ts)``. Drives ``meho conventions history
      <slug>`` (per-convention chronological scan) and "last N edits
      for this convention" probes without an extra ORDER BY sort.

    The model deliberately ships with no helper methods; history rows
    are write-once + read-mostly (T3's CLI is the only consumer in
    v0.2) and the query patterns live at the call site.
    """

    __tablename__ = "tenant_convention_history"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NOT NULL -- every history row attaches to exactly one
    # convention. Soft FK (no REFERENCES clause) per the issue body's
    # explicit choice; T2's CRUD enforces referential integrity at
    # insert time.
    convention_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        nullable=False,
    )
    # Nullable -- the first history row (CREATE) has no prior state.
    # T2's PATCH route copies the existing ``body`` into this column
    # before writing the new ``body_after``.
    body_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_after: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL -- every history row must record who made the change.
    # T5's seed migration uses a synthetic sub (``"system:seed"``) for
    # the initial seed rows.
    actor_sub: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    # Nullable -- migration-seeded rows have no audit_log row to point
    # at. T2's CRUD writes populate ``audit_id`` from the audit
    # middleware's contextvar so G8's audit-query path can join
    # history back to the originating request.
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "tenant_convention_history_convention_idx",
            "convention_id",
            "ts",
            postgresql_using="btree",
        ),
    )


class AgentDefinition(Base):
    """A first-class, tenant-scoped definition of an LLM agent MEHO can run.

    G11.1-T2 (#809) under Initiative #802 (the P1 agent runtime). The
    runtime (T1 #808) executes a tool-use loop in MEHO's process; this
    table is what that loop loads to know *which* agent it is running:
    the identity it runs as, the logical model tier, the system prompt,
    the toolset spec, the turn budget, and an optional structured-output
    schema. Storing the definition as a typed row (not an ad-hoc API
    payload) makes agents listable, versionable, and auditable objects.

    Dedicated-table choice
    ----------------------

    Unlike kb / memory (which wrap the shared ``documents`` retrieval
    substrate), an agent definition is a *structured* record with typed
    columns -- an integer turn budget, a bounded model tier, a JSON
    toolset spec -- not a retrievable text blob. The
    :class:`BroadcastOverride` precedent (a dedicated tenant-scoped
    CRUD table with a real FK to ``tenant.id``) is the load-bearing
    shape this model copies.

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. Same portable :class:`Uuid` shape
      every other model uses; PG gets ``gen_random_uuid()`` via the
      migration, the ORM falls back to ``default=uuid.uuid4`` on
      SQLite.
    * ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES
      tenant(id)`` FK. Same precedent as :class:`Document` /
      :class:`BroadcastOverride`: a brand-new table with no chassis-era
      rows and a clean downgrade that drops the whole table can enforce
      the FK at the substrate boundary without a backfill/cascade
      trade-off. An orphan row for a typo'd / deleted / replayed tenant
      id surfaces as :class:`IntegrityError` at insert time rather than
      as a never-resolving definition at run time.
    * ``name`` -- Text NOT NULL. The operator-facing slug
      (``incident-triage``, ``vm-inventory-bot``). Validated against the
      safe-URL alphabet at the API / service layer (it is a URL path
      segment for the REST surface). Unique per tenant via the named
      ``agent_definition_tenant_name_idx``.
    * ``identity_ref`` -- Text NOT NULL. A *reference* to the agent
      principal whose permissions the toolset is intersected with at run
      time (G11.2). Stored as opaque text (a soft reference, no FK) --
      the agent-identity table itself is G11.2-T1's scope, and this
      Task deliberately stores only the reference, not the identity.
    * ``model_tier`` -- Text NOT NULL. A *logical* tier
      (``standard`` / ``fast`` / ``deep``) that G11.5's multi-provider
      resolver maps to a concrete backend at run time. The bounded set
      is enforced at the Pydantic layer (a ``Literal``), not via a DB
      ``CHECK``, so a future tier lands without a migration -- the same
      forward-compat argument :class:`BroadcastOverride.scope_field`
      makes.
    * ``system_prompt`` -- Text NOT NULL. The agent's system prompt.
      Stored as-is; the runtime feeds it to the model verbatim.
    * ``toolset`` -- Portable :class:`JSON` -> :class:`JSONB`, NOT NULL
      DEFAULT ``{}``. The allowed meta-tools / connector-ops spec.
      T3 (#810) resolves it and intersects it with the identity's
      permissions; T2 only stores it. JSON-shaped so the spec can grow
      (allow-lists, glob patterns, per-op arg constraints) without a
      migration.
    * ``turn_budget`` -- Integer NOT NULL. The maximum number of
      model turns the runtime allows before stopping the loop (maps to
      Pydantic AI's ``UsageLimits(request_limit=...)`` in T1). A
      positive-integer floor is enforced at the Pydantic layer.
    * ``output_schema`` -- Portable JSON, *nullable*. An optional JSON
      Schema the runtime uses for structured output (Pydantic AI's
      ``output_type``); ``NULL`` means free-form text output.
    * ``enabled`` -- Boolean NOT NULL DEFAULT ``True``. A soft on/off
      switch so an operator can park a definition without deleting it
      (and the run surface T4 can refuse to start a disabled agent).
    * ``created_by_sub`` -- Text NOT NULL. JWT ``sub`` of the
      tenant-admin who created the definition -- captures authorship
      for the audit trail, mirroring
      :attr:`BroadcastOverride.created_by_sub`.
    * ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL.
      PG-side ``now()`` server defaults via the migration; the ORM also
      declares ``default=lambda: datetime.now(UTC)`` plus
      ``onupdate=lambda: datetime.now(UTC)`` on ``updated_at`` so
      ORM-side edits bump the timestamp.

    Index
    -----

    * ``agent_definition_tenant_name_idx`` -- unique composite b-tree
      on ``(tenant_id, name)``. Enforces per-tenant name uniqueness
      (the natural key for the CRUD upsert / lookup) and drives the
      tenant-scoped list query. Uniqueness is declared exclusively via
      the named index (no per-column ``unique=True``) so PG does not
      auto-create a redundant duplicate -- the
      :class:`Tenant` / :class:`Target` / :class:`BroadcastOverride`
      convention.

    The model deliberately ships with no helper methods -- read / write
    paths live in :mod:`meho_backplane.agents.service`. The ORM class is
    a pure data shape.
    """

    __tablename__ = "agent_definition"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # NOT NULL with a real REFERENCES tenant(id) FK -- see class
    # docstring for the Document / BroadcastOverride precedent rationale.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Soft reference to the G11.2 agent principal -- no FK (that table is
    # G11.2-T1's scope; this Task stores only the reference).
    identity_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # Logical tier ("standard" | "fast" | "deep"); bounded at the
    # Pydantic layer, not via a DB CHECK (forward-compat -- see docstring).
    model_tier: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    toolset: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    turn_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional JSON Schema for structured output; NULL = free-form text.
    output_schema: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
    )
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
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
            "agent_definition_tenant_name_idx",
            "tenant_id",
            "name",
            unique=True,
            postgresql_using="btree",
        ),
    )


class AgentRunStatus(StrEnum):
    """Closed lifecycle status of an :class:`AgentRun`.

    Initiative #802 (G11.1 Agent runtime), Task #813 (T6). The runtime
    hosts an LLM tool-use loop in MEHO's process; every invocation is
    one ``agent_run`` row whose ``status`` walks an explicit, enforced
    state machine. The legal transitions live in
    :data:`meho_backplane.operations.agent_run.ALLOWED_TRANSITIONS`; the
    service rejects any edge not on that map so an illegal jump (e.g.
    ``succeeded`` -> ``running``) cannot land in the DB.

    Members:

    * :attr:`PENDING` -- the row was created but the loop has not
      started executing yet (initial state on insert).
    * :attr:`RUNNING` -- the loop is executing tool-use turns.
    * :attr:`AWAITING_APPROVAL` -- the loop is paused on a
      policy-gated tool call whose verdict is ``needs-approval``
      (G11.2 resolves the verdict; the runtime parks the run here in
      the meantime). Resumable back to ``running``.
    * :attr:`SUCCEEDED` -- the loop completed and produced ``output``
      (terminal).
    * :attr:`FAILED` -- the loop errored or exhausted its turn budget
      without producing a usable result (terminal).
    * :attr:`CANCELLED` -- an authorized operator cancelled a
      non-terminal run (terminal). The cancellation path is the
      ``running`` / ``pending`` / ``awaiting_approval`` ->
      ``cancelled`` edge.

    Mirrors the closed-enum + DB ``CHECK`` discipline
    :class:`GraphEdgeKind` / :class:`GraphHistoryChangeKind` set: the
    enum and the ``CHECK (status IN (...))`` constraint move in
    lock-step via migration ``0015``; the drift guard
    :func:`tests.test_db_agent_run.test_status_check_matches_enum`
    enforces the equality at unit-test time.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunTrigger(StrEnum):
    """Closed enum of what initiated an :class:`AgentRun`.

    Initiative #802 (G11.1 Agent runtime), Task #813 (T6). Records the
    provenance of a run so audit / replay (G11.4/C2) can answer "why did
    this agent run". The vocabulary is closed; widening it is a
    coordinated DB + model change (new migration, new member, new
    ``CHECK`` body) so the enum and the constraint never drift.

    Members:

    * :attr:`DIRECT` -- an operator invoked the run synchronously or via
      the async handle surface (G11.1-T4).
    * :attr:`SCHEDULED` -- the scheduler (G11.3) fired the run off a
      cron / one-off trigger.
    * :attr:`EVENT` -- an event trigger (G11.3 transactional outbox)
      fired the run.
    * :attr:`AGENT_INVOKED` -- another agent invoked this run as a child
      (G11.1-T5 agent-invokes-agent composition); the parent run's id is
      carried on :attr:`AgentRun.parent_run_id`.
    """

    DIRECT = "direct"
    SCHEDULED = "scheduled"
    EVENT = "event"
    AGENT_INVOKED = "agent-invoked"


#: Closed enum of :attr:`AgentRun.status` -- the v0.2 six-state
#: lifecycle vocabulary. Derived from :class:`AgentRunStatus` so the enum
#: and the DB-layer ``CHECK`` constraint cannot drift; the drift guard in
#: :mod:`tests.test_db_agent_run` enforces the equality at unit-test time.
_AGENT_RUN_STATUSES: tuple[str, ...] = tuple(s.value for s in AgentRunStatus)

#: Closed enum of :attr:`AgentRun.trigger` -- the four provenance kinds.
#: Derived from :class:`AgentRunTrigger`; same lock-step / drift-guard
#: discipline as :data:`_AGENT_RUN_STATUSES`.
_AGENT_RUN_TRIGGERS: tuple[str, ...] = tuple(t.value for t in AgentRunTrigger)

#: Closed enum of :attr:`AgentRun.in_flight_policy` -- the per-run
#: snapshot of the firing trigger's :class:`ScheduledTriggerInFlightPolicy`
#: copied at run-start so a mid-flight definition edit cannot flip
#: behavior on a run that's already executing (T4 #825). Frozen literal
#: tuple here (not derived from the enum class) because
#: :class:`ScheduledTriggerInFlightPolicy` is defined further down in
#: the module file -- reshuffling the file to import-order matters less
#: than keeping the closed-vocab snapshot self-contained and the
#: file-order grouping (agent-runtime models together, scheduler models
#: together) intact. The drift guard
#: :func:`tests.test_db_agent_run.test_in_flight_policy_check_matches_scheduled_trigger_enum`
#: asserts this tuple matches :class:`ScheduledTriggerInFlightPolicy`
#: at unit-test time so the two cannot silently drift.
_AGENT_RUN_IN_FLIGHT_POLICIES: tuple[str, ...] = (
    "resume",
    "fail_into_audit",
)

#: Per-run default for :attr:`AgentRun.in_flight_policy`. Mirrors
#: :class:`ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT` -- the
#: conservative outcome the consumer doc (``agent-runtime-for-ops-spec.md``
#: §P2) explicitly accepts. Operators opt into ``resume`` per agent
#: definition; the scheduler then copies the value into the run row
#: at run-start.
_AGENT_RUN_IN_FLIGHT_POLICY_DEFAULT: str = "fail_into_audit"


class AgentRun(Base):
    """One row per LLM-agent invocation hosted in MEHO's process.

    Initiative #802 (G11.1 Agent runtime), Task #813 (T6). The runtime
    executes an agent's tool-use loop in-process (G11.1-T1); each
    invocation is one durable ``agent_run`` row that ties a session's
    tool calls together, makes the run inspectable + cancellable, and
    seeds the audit/replay lineage. The row's :attr:`id` **is** the
    ``agent_session_id`` lineage key that G11.4/C2 binds into every
    per-tool-call audit row (mirroring :attr:`AuditLog.agent_session_id`,
    migration ``0014``).

    The lifecycle (``status``) is an explicit, enforced state machine --
    illegal transitions are rejected by
    :mod:`meho_backplane.operations.agent_run`, not silently written.
    This model is **storage-only**: no helper / transition logic lives on
    the class (the discipline :class:`AuditLog` / :class:`WebSession`
    follow); the lifecycle service owns every mutation.

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. PG production gets
      ``gen_random_uuid()`` via migration ``0015``; :func:`uuid.uuid4`
      is the ORM default for the SQLite dev/test path and out-of-band
      inserts. This id doubles as the ``agent_session_id`` lineage key,
      so it must be globally unique without a central allocator -- UUID
      is the chassis-wide answer (``audit_log.id``, ``web_session.id``).

    * ``agent_definition_id`` -- UUID nullable, **soft-FK**. Points at
      the ``agent_definition`` row (G11.1-T2 / #809) the run executed.
      No FK clause in v0.2: the ``agent_definition`` table lands in a
      sibling task in parallel, so a hard FK here would couple the two
      migrations' ordering; the soft-FK discipline
      (:attr:`AuditLog.target_id`) keeps this migration independently
      reversible. Nullable because an ad-hoc run (no stored definition)
      is a legitimate early-runtime shape.

    * ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES
      tenant(id)`` FK. ``agent_run`` is a brand-new clean-slate
      substrate (no chassis-era rows), so the FK is enforced at the DB
      layer -- same discipline :class:`GraphNode` / :class:`Document`
      follow. No ``ondelete``: tenant deletion is a major operation that
      must clear the tenant's runs first (default ``NO ACTION`` blocks
      the cascade).

    * ``identity_sub`` / ``identity_act`` -- Text. The RFC 8693
      delegation pair: ``sub`` is the principal the agent acts *for*
      (the human / service operator), ``act`` is the agent principal
      acting on their behalf. ``act`` is nullable because a run invoked
      directly by a human (no delegation) leaves it NULL; ``sub`` is
      NOT NULL because every run has a responsible principal. Mirrors
      :attr:`AuditLog.operator_sub` (the chassis has no ``operator``
      table; the Keycloak ``sub`` is the stable identifier end-to-end).

    * ``trigger`` -- Text NOT NULL with a DB-layer ``CHECK trigger IN
      (...)`` constraint (see :data:`_AGENT_RUN_TRIGGERS`). Closed enum
      (:class:`AgentRunTrigger`).

    * ``model_tier`` -- Text NOT NULL. The *logical* tier the operator
      requested (e.g. ``cheap`` / ``deep``); the multi-provider resolver
      (G11.5) maps it to a concrete provider + model. Free-text, not a
      closed enum: the tier vocabulary is consumer-defined (the harness
      names its own tiers) and MEHO does not enumerate it.

    * ``provider`` / ``model`` -- Text nullable. The *resolved*
      provider (e.g. ``anthropic``) and model id (e.g.
      ``claude-...``) the run actually executed against. Nullable
      because they are unknown until the resolver runs (a ``pending``
      run has not resolved them yet); the runtime populates them at
      ``start`` time.

    * ``status`` -- Text NOT NULL with a DB-layer ``CHECK status IN
      (...)`` constraint (see :data:`_AGENT_RUN_STATUSES`). Closed enum
      (:class:`AgentRunStatus`). Defaults to ``pending`` on insert.

    * ``turns`` -- Integer NOT NULL, default 0. The count of tool-use
      turns the loop has executed. The runtime increments it per turn;
      the turn budget (``UsageLimits.request_limit`` in G11.1-T1) is
      enforced by the loop, not this column -- ``turns`` is the
      observable counter.

    * ``cost`` -- ``Numeric(12, 6)`` nullable. **Stub until G11.5/C3**:
      the column is recorded here so C3 can populate per-identity cost
      attribution without a follow-up migration, but the runtime writes
      NULL in v0.2 (cost computation is explicitly out of scope for this
      Task). ``Numeric`` (not float) because cost is money-shaped --
      exact decimal arithmetic, no binary-float rounding drift. Six
      fractional digits cover sub-cent token pricing.

    * ``output`` -- portable JSON nullable. The run's final result
      (structured output when the agent declared an ``output_type``, or
      a ``{"text": ...}`` projection otherwise). NULL until the run
      reaches a terminal state with a result.

    * ``error`` -- Text nullable. A human-readable failure reason on a
      ``failed`` run (the exception class + message the loop surfaced);
      NULL otherwise. Kept distinct from ``output`` so a failed run's
      diagnostics do not masquerade as a result.

    * ``parent_run_id`` -- UUID nullable, soft-FK to this table's own
      ``id``. Populated when this run was invoked by another agent
      (``trigger='agent-invoked'``, G11.1-T5): the child row points at
      the parent run's id, so the composition tree is walkable. Soft-FK
      (no clause) -- same self-referential discipline
      :attr:`AuditLog.parent_audit_id` follows.

    * ``created_at`` -- ``timestamptz`` NOT NULL. PG-side ``now()``
      server default; ORM ``default=lambda: datetime.now(UTC)`` for
      SQLite.

    * ``started_at`` / ``ended_at`` -- ``timestamptz`` nullable.
      ``started_at`` is set when the run transitions ``pending`` ->
      ``running``; ``ended_at`` when it reaches any terminal state.
      Both NULL until those transitions fire -- the lifecycle service
      stamps them.

    * ``lease_owner`` -- Text nullable. Initiative #804 (G11.3
      Scheduler), Task #825 (T4). The worker process / replica
      identifier that holds the lease on this run while it executes.
      NULL when no worker is executing the run (``pending`` /
      ``awaiting_approval`` after a release / any terminal state). A
      non-NULL value means "this worker is responsible for advancing
      the run". The reaper consults it for diagnostics; the *expiry*
      column drives reclaim.

    * ``lease_expires_at`` -- ``timestamptz`` nullable. The wall-clock
      after which the lease is considered abandoned (the worker died,
      a pod was OOM-killed, the network partitioned). The reaper
      (``meho_backplane.scheduler.reaper``) scans
      ``status='running' AND lease_expires_at < now()`` and applies
      :attr:`in_flight_policy`. The healthy worker bumps this
      forward periodically via the lifecycle service's
      ``heartbeat`` -- as long as heartbeats land, the reaper never
      sees the run. NULL whenever ``lease_owner`` is NULL; the
      lifecycle service keeps both columns in lock-step.

    * ``in_flight_policy`` -- Text NOT NULL with a DB-layer ``CHECK
      in_flight_policy IN (...)`` constraint enforcing the closed
      :class:`ScheduledTriggerInFlightPolicy` vocabulary. Per-run
      snapshot of the trigger's policy copied at run-start (T4 #825),
      so a definition edit mid-flight cannot flip behavior on a run
      that's already executing. Defaults to ``fail_into_audit`` --
      the conservative outcome the consumer doc explicitly accepts.
      ``direct`` and ``agent-invoked`` runs (no scheduler trigger)
      take the default; they cannot resume regardless because nothing
      will re-fire them.

    Indexes
    -------

    * ``agent_run_tenant_created_at_idx`` -- composite b-tree on
      ``(tenant_id, created_at)``. Drives the "list runs for tenant X,
      newest first" inspection surface (G11.1-T4).
    * ``agent_run_status_idx`` -- b-tree on ``status``. Drives the
      "find all running / awaiting-approval runs" query an operator
      needs to inspect / cancel in-flight work.
    * ``agent_run_parent_run_id_idx`` -- b-tree on ``parent_run_id``.
      Drives the composition-tree walk (children of a parent run,
      G11.1-T5).
    * ``agent_run_lease_expires_at_idx`` -- partial b-tree on
      ``lease_expires_at`` (PG ``WHERE status='running'``). Drives
      the reaper's "what leases have expired" query without
      scanning the table. T4 #825.
    * ``agent_run_tenant_work_ref_idx`` -- composite b-tree on
      ``(tenant_id, work_ref)``. Drives the tenant-scoped exact-match
      ``--work-ref`` filter on the agent-run list (work_ref I3-T2 #1662).
    """

    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Soft-FK to agent_definition.id (table lands in parallel #809) --
    # see class docstring for why no FK clause in v0.2.
    agent_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # RFC 8693 delegation pair. ``sub`` = principal acted for (required);
    # ``act`` = agent principal acting (NULL when a human invokes
    # directly with no delegation).
    identity_sub: Mapped[str] = mapped_column(Text, nullable=False)
    identity_act: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    model_tier: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    model: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=AgentRunStatus.PENDING.value,
    )
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Stub until G11.5/C3 -- recorded NULL in v0.2 (cost compute is out
    # of scope for #813). Numeric (not float) -- cost is money-shaped.
    cost: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
        default=None,
    )
    output: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Self-referential soft-FK -- set on agent-invoked child runs
    # (G11.1-T5). Same discipline as audit_log.parent_audit_id.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Initiative #804 (G11.3 Scheduler), Task #825 (T4) -- lease /
    # heartbeat / per-run in-flight-policy snapshot. ``lease_owner`` +
    # ``lease_expires_at`` are kept in lock-step by the lifecycle
    # service (both set together at claim, both cleared together at
    # release). ``in_flight_policy`` is the per-run snapshot of the
    # firing trigger's policy; defaults to ``fail_into_audit`` (the
    # conservative outcome the consumer doc accepts).
    lease_owner: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    in_flight_policy: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=_AGENT_RUN_IN_FLIGHT_POLICY_DEFAULT,
    )
    # External change-ticket reference (work_ref I3-T2 #1662). The opaque
    # cross-system reference (a GitHub issue ``"gh:evoila/meho#11"``, a
    # Jira key, a CR id) of the change record this run works under. Set at
    # create time from the request-time ``work_ref_var`` binding (same
    # ContextVar mechanism as run_id / audit_log.work_ref); set-at-create-
    # only -- never re-mutated. Distinct from ``id`` / the
    # ``agent_session_id`` lineage key: ``id`` is the run's own identity,
    # ``work_ref`` is an external reference set from outside the run. NULL
    # when no work_ref is bound (pre-#1662 rows, direct runs without a
    # ticket). No FK -- opaque cross-system string. Added by migration
    # ``0041``.
    work_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "agent_run_tenant_created_at_idx",
            "tenant_id",
            "created_at",
            postgresql_using="btree",
        ),
        # work_ref I3-T2 #1662 -- composite (tenant_id, work_ref) drives
        # the tenant-scoped exact-match ``--work-ref`` filter the
        # agent-run list surfaces. Mirrors runbook_runs_tenant_work_ref_idx.
        Index(
            "agent_run_tenant_work_ref_idx",
            "tenant_id",
            "work_ref",
            postgresql_using="btree",
        ),
        Index(
            "agent_run_status_idx",
            "status",
            postgresql_using="btree",
        ),
        Index(
            "agent_run_parent_run_id_idx",
            "parent_run_id",
            postgresql_using="btree",
        ),
        # T4 #825 -- the reaper's claim query is
        # ``WHERE status='running' AND lease_expires_at < now()``.
        # The full index drives the lookup on SQLite (which ignores
        # the postgresql_where); the partial index on PG keeps the
        # index narrow (terminal-state and ``pending`` rows are
        # excluded since they have ``lease_expires_at IS NULL`` and
        # the partial predicate filters them anyway).
        Index(
            "agent_run_lease_expires_at_idx",
            "lease_expires_at",
            postgresql_using="btree",
            postgresql_where=sa.text("status = 'running'"),
        ),
        sa.CheckConstraint(
            _ck_in("status", _AGENT_RUN_STATUSES),
            name="ck_agent_run_status",
        ),
        sa.CheckConstraint(
            _ck_in("trigger", _AGENT_RUN_TRIGGERS),
            name="ck_agent_run_trigger",
        ),
        sa.CheckConstraint(
            _ck_in("in_flight_policy", _AGENT_RUN_IN_FLIGHT_POLICIES),
            name="ck_agent_run_in_flight_policy",
        ),
    )


class AgentPrincipal(Base):
    """A MEHO-managed agent principal — a Keycloak client tagged ``kind=agent``.

    G11.2-T1 (#815) under Initiative #803 (G11.2 Agent identity + RBAC +
    approval). Each row represents one agent identity registered by the
    ``meho agent-principal register`` lifecycle verb. The row's lifecycle
    mirrors the Keycloak client it shadows:

    * **register** creates the Keycloak client (confidential,
      service-accounts-enabled, ``kind=agent`` attribute) and inserts
      this row.
    * **revoke** sets ``enabled=false`` on the Keycloak client (kill
      switch; new token grants are refused immediately) and marks
      ``revoked=true`` on this row. The row is never hard-deleted so
      the audit trail stays intact.

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key; PG ``gen_random_uuid()`` via migration
      ``0018``; ORM ``default=uuid.uuid4`` for SQLite / out-of-band inserts.

    * ``tenant_id`` -- UUID NOT NULL, FK to ``tenant.id``. An agent
      principal must belong to a real tenant — there is no out-of-band
      path that would create an orphan row, and the ``NO ACTION`` default
      FK prevents deleting a tenant that still has live agent principals.

    * ``name`` -- Text NOT NULL. The operator-facing handle (e.g.
      ``incident-triage``). Unique within a tenant (enforced by
      ``agent_principal_tenant_name_idx``). Mirrors the naming discipline
      for ``agent_definition.name`` (G11.1-T2).

    * ``keycloak_client_id`` -- Text NOT NULL UNIQUE. The OAuth
      ``clientId`` in Keycloak — conventionally ``agent:<name>`` to keep
      agent clients visually distinct in the Admin Console from user/
      service clients. Globally unique across all tenants (Keycloak has
      no per-realm per-tenant namespace for client ids).

    * ``keycloak_internal_id`` -- Text NOT NULL. Keycloak's internal UUID
      for the client (the ``id`` field in the admin representation,
      distinct from ``clientId``). Used by the revoke path to issue the
      ``PUT /clients/{id}`` call without first doing a lookup-by-clientId.

    * ``owner_sub`` -- Text NOT NULL. The ``sub`` of the operator who
      registered this principal. Never nullable: every agent must have an
      owner (the NHI governance kill-switch model; see Initiative #803).

    * ``revoked`` -- Boolean NOT NULL DEFAULT false. Set to ``true`` by
      the revoke path alongside the Keycloak ``enabled=false`` call.
      Revoked principals are excluded from list results by default but
      the row stays for audit traceability.

    * ``created_by_sub`` -- Text NOT NULL. Operator sub at insert time.
      Distinct from ``owner_sub``: the owner is the long-term responsible
      party; ``created_by_sub`` is the identity that pressed the button.
      For the initial register they are the same; a future reassignment
      path may differ.

    * ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL. PG
      server defaults; ORM ``lambda: datetime.now(UTC)`` for SQLite.

    Indexes
    -------

    * ``agent_principal_tenant_name_idx`` -- unique composite b-tree on
      ``(tenant_id, name)``. Enforces per-tenant name uniqueness and
      drives the tenant-scoped list query.
    * ``agent_principal_keycloak_client_id_idx`` -- unique b-tree on
      ``keycloak_client_id``. Required for the revoke-by-name path and
      for the Keycloak-side uniqueness invariant.
    """

    __tablename__ = "agent_principal"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK to tenant.id -- brand-new table, no chassis-era rows.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    keycloak_client_id: Mapped[str] = mapped_column(Text, nullable=False)
    keycloak_internal_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_sub: Mapped[str] = mapped_column(Text, nullable=False)
    revoked: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=False,
    )
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
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
        Index(
            "agent_principal_tenant_name_idx",
            "tenant_id",
            "name",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "agent_principal_keycloak_client_id_idx",
            "keycloak_client_id",
            unique=True,
            postgresql_using="btree",
        ),
    )


class RunnerPrincipal(Base):
    """A satellite runner's service principal — a Keycloak client tagged ``kind=runner``.

    Initiative #2415 (#2502) under Goal #221. Each row represents one
    satellite runner identity registered by ``meho runner-principal
    register``. It is the direct structural twin of
    :class:`AgentPrincipal` (same columns, same two-index shape,
    same register/revoke lifecycle contract) — the runner lifecycle is
    moulded on the agent lifecycle (#815) — but carves out a distinct
    identity kind with a **read-only** credential scope:

    * **register** creates a Keycloak client (confidential,
      service-accounts-enabled, ``kind=runner`` attribute) whose access
      token carries ``principal_kind=runner``, ``tenant_role=read_only``,
      and a hardcoded ``runner_id=<this row's id>`` mapper, then inserts
      this row with an explicit ``id`` equal to that ``runner_id``.
    * **revoke** sets ``enabled=false`` on the Keycloak client (kill
      switch) then marks ``revoked=true`` on this row. The row is never
      hard-deleted so the audit trail stays intact.

    Why a separate table rather than a ``kind`` column on
    ``agent_principal``: the negative route cage
    (:func:`~meho_backplane.middleware.verify_jwt_and_bind`) and the
    gateway guard (:mod:`~meho_backplane.auth.runner_guard`) reason about
    runners as a first-class identity with its own name→id binding; a
    dedicated table keeps the unique ``(tenant_id, name)`` runner
    namespace independent of the agent namespace and lets #2499/#2501
    reference a runner by ``runner_name`` soft-FK without colliding with
    agent names.

    Columns mirror :class:`AgentPrincipal`; see that class for the
    per-column rationale. The wire/route identity across the gateway set
    is the principal **name** (``{runner}`` path segment in #2498,
    ``?runner=`` in #2499), while the unforgeable token claim carries this
    row's ``id`` — :func:`~meho_backplane.auth.runner_guard.assert_runner_scope`
    is the single point that binds the two.
    """

    __tablename__ = "runner_principal"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK to tenant.id -- brand-new table, no chassis-era rows.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    keycloak_client_id: Mapped[str] = mapped_column(Text, nullable=False)
    keycloak_internal_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_sub: Mapped[str] = mapped_column(Text, nullable=False)
    revoked: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=False,
    )
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
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
        Index(
            "runner_principal_tenant_name_idx",
            "tenant_id",
            "name",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "runner_principal_keycloak_client_id_idx",
            "keycloak_client_id",
            unique=True,
            postgresql_using="btree",
        ),
    )


# ---------------------------------------------------------------------------
# Initiative #2415 (#2499) — gateway assignment + result-ingest storage
# ---------------------------------------------------------------------------


#: Closed set of runner-reported result statuses, mirrored in the
#: ``runner_check_results.status`` CHECK constraint. Tri-state to match
#: the ``runner/wire.py`` ``RunnerResult.status`` vocabulary (#2497): a
#: handler that ran (``ok``), a runner that declined an unsafe item
#: (``refused``), or a handler that raised (``error``). A bare
#: ``ok``/``error`` CHECK would reject the ``refused`` rows #2497's runner
#: legitimately posts.
_RUNNER_RESULT_STATUSES: tuple[str, ...] = ("ok", "refused", "error")


class RunnerAssignmentRow(Base):
    """One satellite runner's current check assignment (Initiative #2415, #2499).

    A single operator-authored document per ``(tenant_id, runner_name)``:
    the ``PUT /api/v1/checks/assignment/{runner}`` route replaces the row
    wholesale. ``items`` stores the *authored* checks
    (``check_ref`` / ``target_name`` / ``op`` / ``params`` /
    ``cadence_seconds``) as JSONB; the runner-facing ``GET`` materialises
    each authored item into a wire ``RunnerWorkItem`` at request time
    (resolving the live target descriptor + the op's ``handler_ref`` /
    ``safety_level``), so target-row drift is picked up on the next poll
    rather than frozen at authoring time.

    ``runner_name`` is a soft-FK to :attr:`RunnerPrincipal.name` (no DB
    FK — the same soft-reference discipline the gateway set uses so
    #2499/#2501 reference a runner by name without coupling to the
    principal table's lifecycle). ``tenant_id`` **is** a real
    ``REFERENCES tenant(id)`` FK: a brand-new clean-slate table, mould
    parity with :class:`RunnerPrincipal` (#2502).
    """

    __tablename__ = "runner_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK -- clean-slate table, mould parity with runner_principal.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # Soft-FK to runner_principal.name (no DB FK): the gateway set keys
    # runners by name and references them across #2499/#2501 by name.
    runner_name: Mapped[str] = mapped_column(Text, nullable=False)
    items: Mapped[list[dict[str, object]]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=list,
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
    )

    __table_args__ = (
        # One assignment document per runner within a tenant; the upsert
        # path keys on this pair.
        Index(
            "runner_assignments_tenant_runner_idx",
            "tenant_id",
            "runner_name",
            unique=True,
            postgresql_using="btree",
        ),
    )


class RunnerCheckResult(Base):
    """One ingested runner check-execution report (Initiative #2415, #2499).

    Persisted by ``POST /api/v1/checks/results`` — one row per accepted
    result in the runner's batch. ``received_at`` is stamped by the
    central clock at ingest (never accepted from the client), because the
    dead-man's switch (#2501) flips workloads stale on the central clock.

    Idempotency: ``(tenant_id, runner_name, result_uid)`` is unique, so a
    re-POST from the runner's on-disk retry spool (#2497) inserts nothing
    and is reported as a duplicate rather than double-counted.
    ``check_ref`` is an opaque per-item string (a soft reference — a
    Sensor UUID from #2416 may ride in it later, with no FK), and the
    ``(tenant_id, runner_name, check_ref, received_at)`` index serves
    #2501's per-check staleness reads.

    ``tenant_id`` is a real ``REFERENCES tenant(id)`` FK (clean-slate
    table, mould parity with :class:`RunnerPrincipal`).
    """

    __tablename__ = "runner_check_results"

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
    runner_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Runner-generated uuid4 hex: the dedup key that makes spool re-posts
    # idempotent.
    result_uid: Mapped[str] = mapped_column(Text, nullable=False)
    check_ref: Mapped[str] = mapped_column(Text, nullable=False)
    op_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Runner-level tri-state (ok / refused / error); see
    # ``_RUNNER_RESULT_STATUSES``.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # The handler's structured payload (a failed probe is still a result,
    # not a runner error). Nullable: refused/error rows carry none.
    result_payload: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Central-stamped at ingest -- NOT accepted from the client.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Ingest idempotency: a re-posted spool batch collides here and is
        # counted as a duplicate.
        Index(
            "runner_check_results_uid_idx",
            "tenant_id",
            "runner_name",
            "result_uid",
            unique=True,
            postgresql_using="btree",
        ),
        # #2501 staleness reads: latest result per (runner, check).
        Index(
            "runner_check_results_staleness_idx",
            "tenant_id",
            "runner_name",
            "check_ref",
            "received_at",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'refused', 'error')",
            name="ck_runner_check_results_status",
        ),
    )


# ---------------------------------------------------------------------------
# G11.2-T3 — per-(principal, op, target) permission model
# ---------------------------------------------------------------------------


class PermissionVerdict(StrEnum):
    """Three-state verdict returned by the permission resolver.

    :attr:`AUTO_EXECUTE` — the op proceeds without human review.
    :attr:`NEEDS_APPROVAL` — the op is parked pending an operator
    decision; the approval queue (G11.2-T4) handles the resume path.
    :attr:`DENY` — the op is refused immediately with a structured,
    agent-reasonable error.

    The vocabulary is intentionally closed: a fourth verdict would
    require a code + migration change, which is the cheapest way to
    prevent drift between the DB layer and the policy engine.
    """

    AUTO_EXECUTE = "auto-execute"
    NEEDS_APPROVAL = "needs-approval"
    DENY = "deny"


#: Closed tuple mirrored in the migration's CHECK constraint.
_PERMISSION_VERDICTS: tuple[str, ...] = tuple(v.value for v in PermissionVerdict)


class AgentPermission(Base):
    """Per-(principal, op-pattern, target-scope) permission grant row.

    G11.2-T3 (#820) under Initiative #803 (the P3 agent identity +
    RBAC + approval gate). One row grants a *principal* (identified by
    their JWT ``sub``) a specific *verdict* for dispatches that match
    an *op_pattern* on a *target_scope*.

    Design decisions
    ----------------

    **Principal is ``sub``, not a dedicated table row.** T1 (#815) adds
    Keycloak-registered agent principals (:class:`AgentPrincipal`); this
    table keeps a *soft* reference on ``principal_sub`` (the JWT ``sub``)
    rather than an FK, consistent with the soft-FK discipline on
    :attr:`AgentDefinition.identity_ref`. The same stable ``sub`` claim
    keys grants for humans and agents alike; the resolver only consults
    agent grants for principals whose token carries
    ``principal_kind=agent`` (see :mod:`meho_backplane.auth.permissions`).

    **op_pattern is an fnmatch glob string.** Examples: ``"*"`` (every
    op), ``"GET:/api/vcenter/*"`` (all vCenter GET ops), ``"vault.kv.*"``
    (all vault kv ops). The resolver scores patterns by the literal
    prefix before the first glob metacharacter and prefers the most
    specific match; ties fold to the most-restrictive verdict
    (fail-closed).

    **target_scope is NOT NULL, defaulting to ``"*"``.** ``"*"`` means
    "any target"; any other value is a target UUID string the grant is
    scoped to. Storing ``"*"`` rather than ``NULL`` for the any-target
    case keeps the uniqueness key total — a ``NULL`` here would let
    Postgres treat two otherwise-identical any-target rows as distinct
    (``NULL != NULL``) and silently defeat
    ``uq_agent_permission_grant``.

    **verdict drives the policy engine.** The resolver returns the
    most-specific matching row's :class:`PermissionVerdict`. When no row
    matches, the default comes from the op's ``safety_level`` (``safe`` →
    ``auto-execute``; ``caution`` → ``needs-approval``; ``dangerous`` →
    ``deny``). The ``safety_level`` ceiling can *tighten* a grant but
    never loosen it past the per-level cap (``caution`` and ``dangerous``
    grants are capped at ``needs-approval`` — a destructive op is never
    auto-executed, but it *is* grantable up to human approval).

    **Tenant scoping.** Every row is owned by a tenant; the resolver
    only matches rows for the requesting operator's tenant.

    Schema decisions
    ----------------

    * ``id`` — UUID primary key. Same portable :class:`Uuid` shape.
    * ``tenant_id`` — UUID NOT NULL with a real ``REFERENCES tenant.id``
      FK. Clean-slate table; same rationale as :class:`AgentDefinition`.
    * ``principal_sub`` — Text NOT NULL. JWT ``sub`` of the principal.
    * ``op_pattern`` — Text NOT NULL. fnmatch glob; ``"*"`` is a valid
      catch-all grant.
    * ``target_scope`` — Text NOT NULL, default ``"*"``. ``"*"`` = any
      target; otherwise a target UUID string.
    * ``verdict`` — Text NOT NULL, DB-layer ``CHECK`` against the three
      closed values.
    * ``created_by_sub`` — Text NOT NULL. JWT ``sub`` of the
      tenant-admin who created the row.
    * ``created_at`` / ``updated_at`` — ``timestamptz`` NOT NULL.

    Indexes / constraints
    ---------------------

    * ``agent_permission_tenant_principal_idx`` — b-tree on
      ``(tenant_id, principal_sub)`` — the dominant query: "all grants
      for principal P in tenant T."
    * ``uq_agent_permission_grant`` — UNIQUE on ``(tenant_id,
      principal_sub, op_pattern, target_scope)`` — the row is *keyed*
      by this tuple; a duplicate would feed a nondeterministic verdict
      selection. The unique index prevents that at the DB layer.

    The model ships no helper methods — the resolver logic lives in
    :mod:`meho_backplane.auth.permissions`.
    """

    __tablename__ = "agent_permission"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real REFERENCES tenant(id) FK -- same rationale as AgentDefinition.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # JWT ``sub`` of the principal being granted the permission.
    # Soft reference -- no FK to the agent principal table (T1's scope).
    principal_sub: Mapped[str] = mapped_column(Text, nullable=False)
    # fnmatch-compatible glob string. "*" = every op.
    op_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    # "*" = any target; UUID string = exactly one target. NOT NULL so
    # the uniqueness key stays total (NULL would defeat the unique index).
    target_scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="*",
    )
    # Three-state verdict: "auto-execute" | "needs-approval" | "deny".
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
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
    # G11.2-T6 (#819) time-bounded elevation. NULL = a permanent grant;
    # a non-null UTC timestamp makes the grant expire — the resolver
    # ignores rows past their ``expires_at`` (reverts automatically) and
    # the grant-expiry sweeper deletes them on its periodic tick.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "agent_permission_tenant_principal_idx",
            "tenant_id",
            "principal_sub",
            postgresql_using="btree",
        ),
        # Drives the elevation-expiry sweeper's "what's expired" scan
        # (G11.2-T6 #819).
        Index(
            "agent_permission_expires_at_idx",
            "expires_at",
            postgresql_using="btree",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_sub",
            "op_pattern",
            "target_scope",
            name="uq_agent_permission_grant",
        ),
        sa.CheckConstraint(
            _ck_in("verdict", _PERMISSION_VERDICTS),
            name="ck_agent_permission_verdict",
        ),
    )


class ScheduledTriggerKind(StrEnum):
    """Closed enum of trigger shapes the G11.3 scheduler dispatches.

    Initiative #804 (G11.3 Scheduler), Task #822 (T1). One
    :class:`ScheduledTrigger` row carries exactly one of three shapes,
    selected by this discriminator; the discriminated-union invariant is
    enforced by the DB-side ``ck_scheduled_trigger_kind_fields`` ``CHECK``
    that pairs each kind with its mandatory column.

    Members:

    * :attr:`CRON` -- recurring trigger driven by a 5-field cron
      expression (``cron_expr`` column). The dispatcher (T2 #823) reads
      the expression via ``croniter`` and materialises ``next_fire_at``.
    * :attr:`ONE_OFF` -- single-shot trigger at a wall-clock time
      (``fire_at`` column). The dispatcher fires it once then marks
      ``status = 'cancelled'`` (or the row stays inactive via
      ``next_fire_at IS NULL``).
    * :attr:`EVENT` -- event-subscription trigger keyed on a JSONB
      filter (``event_filter`` column). The transactional outbox (T3
      #824) matches rows against the filter and dispatches.

    Closed enum -- the vocabulary is fixed at v0.2; widening it is a
    coordinated DB + model change so the enum and the
    :data:`_SCHEDULED_TRIGGER_KINDS` literal (and migration ``0020``'s
    frozen tuple) cannot drift. The lock-step discipline mirrors
    :class:`AgentRunStatus` / :class:`AgentRunTrigger`; the drift guard
    in :mod:`tests.test_db_scheduled_trigger` enforces equality at
    unit-test time.
    """

    CRON = "cron"
    ONE_OFF = "one_off"
    EVENT = "event"


class ScheduledTriggerStatus(StrEnum):
    """Closed lifecycle status of a :class:`ScheduledTrigger`.

    Initiative #804 (G11.3 Scheduler), Task #822 (T1). The admin
    surface (T5 #826) walks triggers through this state machine; the
    dispatcher (T2/T3) only fires rows with :attr:`ACTIVE`.

    Members:

    * :attr:`ACTIVE` -- the trigger is eligible for dispatch.
    * :attr:`PAUSED` -- the trigger is temporarily disabled by an
      operator. ``next_fire_at`` is preserved so resuming reactivates
      without recomputing.
    * :attr:`CANCELLED` -- terminal. The trigger row is retained for
      audit purposes but never fires again.
    * :attr:`FIRED` -- terminal one-off state. Migration ``0025`` (T2
      #823) widened the enum so a one-off trigger transitions
      ``ACTIVE -> FIRED`` after its single dispatch instead of going
      to ``CANCELLED`` (which carries operator-intent semantics).
      :class:`ScheduledTrigger` rows in this state are retained for
      audit (last-fired-at + identity_sub) but never re-dispatched.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FIRED = "fired"


class ScheduledTriggerInFlightPolicy(StrEnum):
    """Closed policy of what happens to a fired run that gets killed mid-flight.

    Initiative #804 (G11.3 Scheduler), Task #822 (T1) for the column;
    T4 #825 owns the resume/fail mechanics. The closed enum keeps the
    storage shape and the dispatch behaviour aligned across the
    Initiative.

    Members:

    * :attr:`RESUME` -- the dispatcher / lease-reaper (T4) attempts to
      resume a killed run from its last recorded state. At-least-once
      semantics; the underlying agent run should be idempotent-friendly.
    * :attr:`FAIL_INTO_AUDIT` -- the killed run is marked ``failed`` with
      a clean audit row explaining the interruption; the next trigger
      tick fires a fresh run. The consumer doc (``agent-runtime-for-ops-
      spec.md`` §P2) explicitly accepts this outcome as the default
      policy -- which is why Option A (extend roll-our-own) is viable at
      all without DBOS-style automatic resume.

    Default at the migration / ORM layer is :attr:`FAIL_INTO_AUDIT` --
    the conservative policy that requires no extra infrastructure (just
    audit) and matches the consumer's accepted-outcome statement.
    Operators opt into :attr:`RESUME` per definition.
    """

    RESUME = "resume"
    FAIL_INTO_AUDIT = "fail_into_audit"


#: Closed enum of :attr:`ScheduledTrigger.kind` -- derived from
#: :class:`ScheduledTriggerKind` so the enum and the DB-layer ``CHECK``
#: constraint cannot drift. The drift guard in
#: :mod:`tests.test_db_scheduled_trigger` enforces equality at
#: unit-test time. Migration ``0020`` records its own frozen literal
#: tuple of the same shape (an independent snapshot).
_SCHEDULED_TRIGGER_KINDS: tuple[str, ...] = tuple(k.value for k in ScheduledTriggerKind)

#: Closed enum of :attr:`ScheduledTrigger.status` -- derived from
#: :class:`ScheduledTriggerStatus`; same lock-step discipline as
#: :data:`_SCHEDULED_TRIGGER_KINDS`.
_SCHEDULED_TRIGGER_STATUSES: tuple[str, ...] = tuple(s.value for s in ScheduledTriggerStatus)

#: Closed enum of :attr:`ScheduledTrigger.in_flight_policy` -- derived
#: from :class:`ScheduledTriggerInFlightPolicy`; same lock-step
#: discipline as :data:`_SCHEDULED_TRIGGER_KINDS`.
_SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES: tuple[str, ...] = tuple(
    p.value for p in ScheduledTriggerInFlightPolicy
)


class ScheduledTrigger(Base):
    """One row per durable trigger that fires a G11.1 agent run.

    Initiative #804 (G11.3 Scheduler), Task #822 (T1). T1 settles the
    durability-substrate fork the Initiative left open: **Option A --
    extend the existing roll-our-own pattern** (asyncio +
    ``pg_try_advisory_lock``; see :mod:`meho_backplane.topology.scheduler`
    and :mod:`meho_backplane.memory.expiry` for the precedent). DBOS
    Transact was the alternative; the decision rationale is recorded in
    the PR body for #822. T2 / T3 / T4 / T5 build on the storage shape
    landed here.

    Single-table discriminated union
    --------------------------------

    A single ``scheduled_trigger`` table stores all three trigger shapes
    because the dispatcher (T2/T3) scans the table with one "claim the
    next due row" query -- splitting into three tables would force three
    scanners with three advisory locks. The shape is the
    discriminated-union pattern: the :attr:`kind` column picks which of
    :attr:`cron_expr` / :attr:`fire_at` / :attr:`event_filter` carries
    the semantics. A DB-side ``CHECK`` constraint
    (``ck_scheduled_trigger_kind_fields``) enforces the invariant -- the
    right column populated, the others NULL -- so a malformed row
    cannot land at the substrate boundary.

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. Same portable :class:`Uuid` shape the
      rest of the chassis uses; PG production gets ``gen_random_uuid()``
      via migration ``0020``, the ORM falls back to
      ``default=uuid.uuid4`` for the SQLite dev/test path.

    * ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES tenant(id)``
      FK. Clean-slate substrate -- no chassis-era rows -- so the FK is
      enforced at the DB layer (same discipline :class:`AgentRun` /
      :class:`AgentDefinition` follow). An orphan trigger for a typo'd /
      replayed tenant id surfaces as :class:`IntegrityError` at insert.

    * ``agent_definition_id`` -- UUID NOT NULL with a real
      ``REFERENCES agent_definition(id) ON DELETE CASCADE`` FK. The
      parent table already exists at HEAD (``0016`` shipped ahead of this
      work), so the FK is tightened here -- the sibling
      :class:`AgentRun` (``0017``) had to use a soft-FK only because its
      migration landed in parallel with ``0016``. A trigger cannot point
      at a definition that no longer exists: deleting the definition
      cascade-deletes its triggers (``ondelete='CASCADE'`` added by
      migration ``0035`` / #1480), so a once-scheduled definition stays
      deletable instead of being pinned by an audit-retained cancelled
      trigger.

    * ``kind`` -- Text NOT NULL with a DB-layer ``CHECK kind IN (...)``
      constraint enforcing the closed
      :class:`ScheduledTriggerKind` vocabulary.

    * ``cron_expr`` -- Text nullable. Populated only when
      ``kind = 'cron'``; the dispatcher (T2 #823) parses it via the
      ``croniter`` library (not added in this Task; T2's dependency).
      The 5-field cron grammar is fixed; storing the literal preserves
      operator intent on read-back.

    * ``fire_at`` -- ``timestamptz`` nullable. Populated only when
      ``kind = 'one_off'``; the dispatcher fires the trigger once at or
      after this wall-clock time, then either cancels the row or marks
      it idle via ``next_fire_at = NULL``.

    * ``event_filter`` -- portable JSON -> JSONB nullable. Populated
      only when ``kind = 'event'``; T3 #824 matches transactional-outbox
      rows against this filter to drive the dispatch. Shape is
      consumer-defined (a typed schema lives at the API layer; the
      column is the forward-compat substrate).

    * ``status`` -- Text NOT NULL with a DB-layer ``CHECK status IN (...)``
      constraint enforcing the closed :class:`ScheduledTriggerStatus`
      vocabulary. Defaults to ``active`` on insert. The dispatcher only
      fires ``active`` rows.

    * ``in_flight_policy`` -- Text NOT NULL with a DB-layer ``CHECK
      in_flight_policy IN (...)`` constraint enforcing the closed
      :class:`ScheduledTriggerInFlightPolicy` vocabulary. Defaults to
      ``fail_into_audit``. T4 #825 owns the dispatch-time policy
      mechanics; this Task only stores the column.

    * ``next_fire_at`` -- ``timestamptz`` nullable. Materialised
      next-fire timestamp the dispatcher claims on (T2/T3). NULL on a
      freshly-created trigger before T2's "compute next" pass runs.
      Indexed (with ``status``) to drive the dispatch claim query.

    * ``last_fired_at`` -- ``timestamptz`` nullable. Set by the
      dispatcher after a successful fire; observable via the admin
      surface (T5).

    * ``created_by_sub`` -- Text NOT NULL. JWT ``sub`` of the
      tenant-admin who created the trigger. The chassis has no
      ``operator`` table; the Keycloak ``sub`` is the stable identifier
      (the precedent :attr:`AgentDefinition.created_by_sub` and
      :attr:`BroadcastOverride.created_by_sub` set).

    * ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL.
      PG-side ``now()`` server defaults via the migration; the ORM also
      declares ``default=lambda: datetime.now(UTC)`` plus
      ``onupdate=lambda: datetime.now(UTC)`` on ``updated_at`` so
      ORM-side edits bump the timestamp.

    Indexes
    -------

    * ``scheduled_trigger_next_fire_at_idx`` -- b-tree on
      ``(status, next_fire_at)`` (partial on PG with
      ``WHERE status = 'active'``). Drives the dispatcher's "what fires
      next" claim query.
    * ``scheduled_trigger_tenant_idx`` -- b-tree on
      ``(tenant_id, kind)``. Drives the admin surface's tenant-scoped
      list (T5 #826) without sequential-scanning the table.

    The model is storage-only -- no helper / transition logic lives on
    the class (the discipline :class:`AgentRun` / :class:`AuditLog` /
    :class:`WebSession` follow). The dispatcher (T2/T3), policy (T4),
    and admin (T5) services own every mutation.
    """

    __tablename__ = "scheduled_trigger"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real REFERENCES tenant(id) FK -- see class docstring.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # Real REFERENCES agent_definition(id) FK -- the parent table exists
    # at HEAD (0016) so this Task tightens the FK 0017 had to leave soft.
    # ``ondelete='CASCADE'`` (migration 0035, #1480): deleting a
    # definition removes its dependent trigger rows -- including a
    # cancelled one cancel() retains for audit -- so a once-scheduled
    # definition is still deletable. The cascade must be the DB-level FK
    # clause, not an ORM ``cascade=`` relationship: the delete path issues
    # a bulk Core ``DELETE`` that bypasses unit-of-work cascades.
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("agent_definition.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Discriminated by ``kind`` -- exactly one populated; the DB-side
    # ``ck_scheduled_trigger_kind_fields`` CHECK enforces the invariant.
    cron_expr: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Per-trigger IANA timezone name. Cron expressions evaluate in this
    # zone via ``zoneinfo.ZoneInfo`` so an operator scheduling
    # ``0 9 * * *`` in ``Europe/Sarajevo`` fires at 09:00 local rather
    # than 09:00 UTC. Migration ``0025`` adds this column with a server
    # default of ``'UTC'`` for the rows shipped by 0020; the ORM-side
    # default keeps fresh inserts on the same backstop.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    fire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # ``none_as_null=True`` is load-bearing: the discriminated-union CHECK
    # ``ck_scheduled_trigger_kind_fields`` predicates on
    # ``event_filter IS NULL`` for the non-event kinds. Without
    # ``none_as_null``, SQLAlchemy's :class:`JSON` type serialises a
    # Python ``None`` as the JSON literal string ``'null'`` (the value
    # stored is non-NULL at the SQL layer), so the CHECK fires when a
    # cron / one_off row leaves ``event_filter`` defaulted. The flag flips
    # the bind-side behaviour to insert SQL ``NULL`` for ``None``, which
    # is what the CHECK expects. The same shape applies on PG's JSONB
    # variant -- the kwarg is forwarded to the underlying type.
    event_filter: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
        default=None,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=ScheduledTriggerStatus.ACTIVE.value,
    )
    in_flight_policy: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
    )
    next_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Skip-state projection (#2327). The tick loop's precondition gate
    # (:func:`~meho_backplane.scheduler.loop._prepare_invocation`) skips a
    # due trigger without advancing its state when the definition is
    # missing/disabled or credentials are unresolved. Before #2327 that
    # skip was invisible on the row -- an operator's ``scheduler list``
    # showed a healthy-looking ``active`` trigger while it silently
    # skipped every 30 s tick for weeks. These three columns project the
    # cumulative skip state onto the row so the read surfaces
    # (``scheduler.list`` / ``scheduler.show`` / the operator console)
    # agree with the pod-log WARNs. Migration ``0057`` adds them.
    #
    # * ``last_skip_reason`` -- the stable machine tag of the most recent
    #   skip cause (``definition_missing`` / ``definition_disabled`` /
    #   ``credentials_unresolved``; a park path also stamps
    #   ``invalid_cron_expr`` / ``unknown_kind``). NULL until the first
    #   skip; cleared back to NULL on the next successful fire.
    last_skip_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    # * ``last_skipped_at`` -- UTC time of the most recent skip. NULL
    #   until the first skip; cleared on the next successful fire.
    last_skipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # * ``skip_count`` -- consecutive skips since the last successful
    #   fire (reset to 0 on the next fire). The loop parks the trigger
    #   (``status='paused'``) once this reaches
    #   :data:`~meho_backplane.scheduler.loop._PARK_AFTER_CONSECUTIVE_SKIPS`
    #   so a permanently-unresolvable trigger stops silently re-tripping
    #   every tick and the state machine itself communicates "broken,
    #   stopped trying". NOT NULL, default 0; migration 0057 backfills
    #   pre-#2327 rows with the server-side ``0`` default.
    skip_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # JSON payload forwarded as the agent run's initial input by the
    # dispatcher (T2 #823). Nullable: a trigger that just kicks off an
    # agent definition with no extra parameters leaves this NULL.
    # ``none_as_null=True`` keeps SQL NULL distinct from the JSON
    # literal ``'null'`` -- the same discipline ``event_filter`` uses.
    inputs: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
        default=None,
    )
    # Identity ``sub`` the dispatcher impersonates when starting the
    # agent run. Distinct from :attr:`created_by_sub` because the
    # operator who created the trigger is not necessarily the identity
    # the scheduler should fire under at runtime (e.g. a service
    # principal). Migration ``0025`` adds this column with a server
    # default of ``'__scheduler__'`` (a sentinel) so the rows shipped
    # by 0020 remain valid; production triggers should set this
    # explicitly at create time.
    identity_sub: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="__scheduler__",
    )
    created_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
    # External change-ticket reference (work_ref I3-T3 #1663). The opaque
    # cross-system reference (a GitHub issue ``"gh:evoila/meho#13"``, a
    # Jira key, a CR id) of the change record this trigger -- and every
    # run it dispatches -- works under. Set at create time; the scheduler
    # binds the shared ``work_ref_var`` ContextVar from this column around
    # each dispatched run so the dispatched ``agent_run.work_ref`` and the
    # run's audit rows inherit the trigger's ref end-to-end
    # (:data:`meho_backplane.operations._audit.work_ref_var`). Triggers
    # have no UPDATE path, so this is set-at-create-only. NULL when no
    # work_ref is bound (pre-#1663 rows). No FK -- opaque cross-system
    # string. Added by migration ``0043``.
    work_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
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
            "scheduled_trigger_next_fire_at_idx",
            "status",
            "next_fire_at",
            postgresql_using="btree",
            postgresql_where=sa.text("status = 'active'"),
        ),
        Index(
            "scheduled_trigger_tenant_idx",
            "tenant_id",
            "kind",
            postgresql_using="btree",
        ),
        # work_ref I3-T3 #1663 -- composite (tenant_id, work_ref) drives
        # the tenant-scoped exact-match ``--work-ref`` filter the
        # scheduled-trigger list surfaces. Mirrors
        # agent_run_tenant_work_ref_idx / runbook_runs_tenant_work_ref_idx.
        Index(
            "scheduled_trigger_tenant_work_ref_idx",
            "tenant_id",
            "work_ref",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            _ck_in("kind", _SCHEDULED_TRIGGER_KINDS),
            name="ck_scheduled_trigger_kind",
        ),
        sa.CheckConstraint(
            _ck_in("status", _SCHEDULED_TRIGGER_STATUSES),
            name="ck_scheduled_trigger_status",
        ),
        sa.CheckConstraint(
            _ck_in("in_flight_policy", _SCHEDULED_TRIGGER_IN_FLIGHT_POLICIES),
            name="ck_scheduled_trigger_in_flight_policy",
        ),
        # Discriminated-union invariant: exactly one of the three
        # discriminator columns carries the semantics, the other two are
        # NULL. The ``(kind = '...' AND col IS NOT NULL AND other IS
        # NULL)`` form is portable across PG and SQLite -- no
        # dialect-specific syntax. The migration's recorded body
        # (_KIND_FIELDS_CHECK in 0020) is the frozen snapshot of the
        # same predicate; the drift guard in
        # :mod:`tests.test_db_scheduled_trigger` asserts equality.
        sa.CheckConstraint(
            (
                "("
                "(kind = 'cron' AND cron_expr IS NOT NULL "
                "AND fire_at IS NULL AND event_filter IS NULL) OR "
                "(kind = 'one_off' AND fire_at IS NOT NULL "
                "AND cron_expr IS NULL AND event_filter IS NULL) OR "
                "(kind = 'event' AND event_filter IS NOT NULL "
                "AND cron_expr IS NULL AND fire_at IS NULL)"
                ")"
            ),
            name="ck_scheduled_trigger_kind_fields",
        ),
    )


# ---------------------------------------------------------------------------
# Approval queue (G11.2-T4 / #817)
# ---------------------------------------------------------------------------


class ApprovalRequestStatus(StrEnum):
    """Closed lifecycle status of an :class:`ApprovalRequest`.

    Initiative #803 (G11.2 Agent permission model), Task #817 (T4). The
    approval queue parks a ``requires_approval`` dispatch durably; the
    row walks a simple four-state lifecycle enforced by the service
    (:mod:`meho_backplane.operations.approval_queue`).

    Members:

    * :attr:`PENDING` -- the request was written but no decision has
      been made (initial state on insert). The associated agent run (if
      any) is in ``awaiting_approval``.
    * :attr:`APPROVED` -- an authorized operator approved the request;
      the dispatcher has re-executed the original call. Terminal.
    * :attr:`REJECTED` -- an authorized operator rejected the request;
      the original call was not executed. Terminal.
    * :attr:`EXPIRED` -- the ``expires_at`` deadline passed without a
      decision; the expiry sweep transitioned the row and wrote the
      decision audit row. Terminal.

    The enum and the ``CHECK (status IN (...))`` constraint on the DB
    table move in lock-step (migration ``0023``); the drift guard
    :func:`tests.test_migration_0023_approval_request.test_status_check_matches_enum`
    asserts equality at unit-test time.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


#: Closed ``approval_request.status`` vocabulary derived from the enum --
#: kept in sync with migration ``0023``'s ``_APPROVAL_REQUEST_STATUSES``
#: literal. The drift guard asserts equality so the two never diverge.
_APPROVAL_REQUEST_STATUSES: tuple[str, ...] = tuple(s.value for s in ApprovalRequestStatus)


class ApprovalRequest(Base):
    """One pending (or decided) approval request for a ``requires_approval`` op.

    Initiative #803 (G11.2 Agent permission model), Task #817 (T4). When
    the policy gate returns ``requires_approval=True`` for an agent
    principal, the dispatcher creates one of these rows instead of
    executing the op. The row parks the dispatch durably (process
    restarts cannot lose it), surfaces the pending request to human
    reviewers, and provides the resume hook: approve → re-dispatch with
    the original params; reject → abort cleanly.

    Two synchronous audit rows accompany every approval request:

    1. A **"request"** audit row written when the pending row is created
       (same transaction). ``method='APPROVAL'``, ``path='approval.request'``.
    2. A **"decision"** audit row written when the request transitions to
       ``approved`` / ``rejected`` / ``expired`` (same transaction). The
       decision row is **not** inserted until the row's status commits —
       mirroring the dispatcher's synchronous-audit invariant.

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. PG-side ``gen_random_uuid()``; ORM
      ``default=uuid.uuid4`` for SQLite.

    * ``tenant_id`` -- UUID NOT NULL, real FK to ``tenant.id``. Clean-
      slate table; hard FK enforced (no ondelete — tenant deletion must
      clear requests first). Same discipline as ``agent_run`` (0017).

    * ``run_id`` -- UUID nullable, soft-FK to ``agent_run.id``. Set when
      the request came from an in-flight agent run; NULL otherwise. Soft
      FK mirrors ``audit_log.agent_session_id`` (0014).

    * ``principal_sub`` / ``principal_act`` -- RFC 8693 ``sub`` / ``act``
      pair from the dispatching operator. ``sub`` NOT NULL (every request
      has a responsible principal); ``act`` nullable (NULL for direct human
      calls without delegation).

    * ``op_id`` / ``connector_id`` -- the operation + connector the
      dispatcher was asked to call. Stored verbatim for resume.

    * ``target_id`` -- UUID nullable, soft-FK (no clause). The target the
      dispatch was scoped to; NULL for tenant-wide ops. Same discipline as
      ``audit_log.target_id`` (0004).

    * ``params_hash`` -- SHA-256 hex hash of the canonicalised params
      (from :func:`~meho_backplane.operations._validate.compute_params_hash`).
      The swap-defence value: a caller-supplied params dict on the REST
      ``/approve`` path is re-hashed against this to detect substitution
      between request and approval.

    * ``params`` -- JSON nullable (JSONB on PG). The original dispatch
      params, stored verbatim (#1503) so a parked **direct** operator op
      approved via ``/decide`` or MCP by-id — surfaces that hold only the
      request id, not the params — can re-dispatch with the stored params
      rather than only recording the decision. Nullable so pre-0036 rows
      remain valid. Internal re-dispatch input; never serialised onto a
      read view or broadcast frame.

    * ``proposed_effect`` -- JSON (JSONB on PG). Human-readable summary of
      what the op would do if approved; populated at queue time; JSONB for
      GIN filtering. NOT NULL, DEFAULT ``{}``.

    * ``status`` -- Closed enum, DB ``CHECK``, default ``'pending'``.

    * ``reviewed_by`` -- Text nullable. ``sub`` of the approver / rejecter.

    * ``decided_at`` -- ``timestamptz`` nullable. Stamped on decision.

    * ``created_at`` -- ``timestamptz`` NOT NULL.

    * ``expires_at`` -- ``timestamptz`` nullable. Expiry deadline; NULL
      means no deadline.

    * ``work_ref`` -- Text nullable. The external change-ticket reference
      that authorised the dispatch (work_ref I2-T1 #1659) -- an opaque
      string such as ``"gh:evoila/meho#1"``, captured at creation from
      :data:`meho_backplane.operations._audit.work_ref_var` (the same
      ContextVar that carries the value onto ``audit_log.work_ref``, 0039
      / #1655). Re-bound from this row on re-dispatch so the approved
      op's audit rows inherit the ref. NULL when no work_ref was bound.
      No FK -- same soft-reference discipline as ``run_id`` / ``target_id``.
      Added by migration ``0040``.

    * ``agent_session_id`` -- UUID nullable, soft-FK into the G8.2 session
      graph (mirrors ``audit_log.agent_session_id``, 0014). The session
      the parking dispatch belonged to, captured at creation via
      :func:`~meho_backplane.operations._audit.resolve_agent_session_id`
      (the agent run id inside an agent loop; the ``Mcp-Session-Id`` for
      a direct MCP operator dispatch). Re-bound from this row on
      re-dispatch so the approved op's audit row anchors in the
      originating session's replay tree (#2086). NULL when the park
      happened outside any session. Added by migration ``0053``.

    * ``request_audit_id`` -- UUID nullable, soft-FK to ``audit_log.id``.
      The primary key of the ``approval.request`` audit row written in
      the same transaction as this row (the durable audit record of the
      parking call). The decision audit rows and the resumed dispatch's
      audit row set their ``parent_audit_id`` to this value, which links
      the park → decide → execute chain into one replay subtree (#2086).
      NULL only on pre-0053 rows. Added by migration ``0053``.

    * ``resumed_at`` -- ``timestamptz`` nullable. The exactly-one-resumer
      claim (#2293): the UTC time the winning resumer claimed the single
      post-approval execution, or NULL while unclaimed. Every dispatcher
      of an approved op (the in-process agent waiter, the shared
      :func:`resume_dispatch_after_approval` operator path, any future
      resumer) must win the atomic claim
      (:func:`~meho_backplane.operations.approval_queue.claim_resume` --
      ``UPDATE ... SET resumed_at = now WHERE resumed_at IS NULL``) before
      it re-dispatches ``_approved=True``; the winner (one row touched)
      executes, a loser (zero rows) no-ops. Set exactly once, never
      cleared (a one-way latch). NULL on pre-0055 rows means "never
      resumed" -- the same claimable starting state a freshly-parked
      request has. No FK, no index. Added by migration ``0055``.

    Indexes
    -------

    * ``approval_request_tenant_created_at_idx`` -- ``(tenant_id, created_at)``.
    * ``approval_request_status_idx`` -- ``status``.
    * ``approval_request_run_id_idx`` -- ``run_id``.
    * ``approval_request_work_ref_idx`` -- ``work_ref``.
    """

    __tablename__ = "approval_request"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK -- clean-slate substrate (see class docstring).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # Soft-FK to agent_run.id -- NULL for non-agent-run requests.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # RFC 8693 delegation pair -- mirrors agent_run.identity_sub / _act.
    principal_sub: Mapped[str] = mapped_column(Text, nullable=False)
    principal_act: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Dispatch coordinates for resume.
    op_id: Mapped[str] = mapped_column(Text, nullable=False)
    connector_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, default=None)
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Original dispatch params, stored verbatim so any approval surface
    # (REST /decide, MCP by-id approve) can re-dispatch a parked *direct*
    # operator op without the approver re-supplying them (#1503). REST
    # /approve still supplies them in-band and verifies them against
    # params_hash; the in-process agent-run resume uses its own in-memory
    # params and ignores this column. Nullable so pre-0036 rows (which
    # have no stored params) stay valid; a row written on or after 0036
    # always carries the params. Internal re-dispatch input only -- never
    # surfaced on a read view or broadcast frame (the swap-defence hash
    # and the redacted proposed_effect remain the reviewer-facing fields).
    params: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    # Proposed effect -- human-readable summary for the reviewer.
    proposed_effect: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=ApprovalRequestStatus.PENDING.value,
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # External change-ticket reference (work_ref I2-T1 #1659). Set at
    # creation from the request-time work_ref_var binding (same ContextVar
    # mechanism as run_id / audit_log.work_ref); re-bound from this row on
    # re-dispatch so the approved op's audit rows inherit the ref. NULL
    # when no work_ref is bound. No FK -- opaque cross-system string.
    # Added by migration 0040.
    work_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    # Session-replay lineage (#2086). The session the parking dispatch
    # belonged to (agent run id, or the MCP session id for a direct
    # operator dispatch), captured at creation via
    # resolve_agent_session_id(); re-bound onto agent_session_id_var on
    # re-dispatch. Soft-FK into the G8.2 session graph -- same
    # discipline as audit_log.agent_session_id (0014). Added by
    # migration 0053.
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # The ``approval.request`` audit row written alongside this row (the
    # durable audit record of the parking call). Decision audit rows and
    # the resumed dispatch's audit row parent-link to it, stitching the
    # park -> decide -> execute chain into one replay subtree (#2086).
    # Soft-FK to audit_log.id. Added by migration 0053.
    request_audit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )
    # Exactly-one-resumer claim (#2293). UTC time the winning resumer
    # claimed the single post-approval execution, or NULL while unclaimed.
    # Every dispatcher of an approved op (the in-process agent waiter, the
    # shared resume_dispatch_after_approval operator path, any future
    # resumer) must win the atomic claim
    # (claim_resume: UPDATE ... SET resumed_at = now WHERE resumed_at IS
    # NULL) before it re-dispatches _approved=True: one row touched wins
    # and executes; zero rows touched loses and no-ops. Set exactly once,
    # never cleared -- a one-way latch, so a failed dispatch is not
    # silently retried into a possible double write. NULL on pre-0055 rows
    # means "never resumed", the same claimable starting state a
    # freshly-parked request has. No FK, no index (read/written only by the
    # primary-key-scoped conditional UPDATE). Added by migration 0055.
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "approval_request_tenant_created_at_idx",
            "tenant_id",
            "created_at",
            postgresql_using="btree",
        ),
        Index(
            "approval_request_status_idx",
            "status",
            postgresql_using="btree",
        ),
        Index(
            "approval_request_run_id_idx",
            "run_id",
            postgresql_using="btree",
        ),
        Index(
            "approval_request_work_ref_idx",
            "work_ref",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            _ck_in("status", _APPROVAL_REQUEST_STATUSES),
            name="ck_approval_request_status",
        ),
    )


# ---------------------------------------------------------------------------
# Gateway command queue (Initiative #2415 / #2498)
# ---------------------------------------------------------------------------


class GatewayCommandStatus(StrEnum):
    """Closed lifecycle status of a :class:`GatewayCommand`.

    Initiative #2415 (Remote execution gateway), Task #2498. The gateway
    command plane parks a centrally-enqueued, pre-authorized operation
    durably; the row walks a simple four-state lifecycle enforced by the
    service (:mod:`meho_backplane.gateway.queue`).

    Members:

    * :attr:`PENDING` -- enqueued centrally, awaiting a runner claim
      (initial state on insert).
    * :attr:`DELIVERED` -- claimed by the runner's long-poll
      (``pending`` flips to ``delivered`` under ``SELECT ... FOR UPDATE
      SKIP LOCKED`` on PG / a conditional ``UPDATE`` on the SQLite test
      path); ``delivered_at`` is stamped. A row that is claimed but never
      reported stays here (lost, not redelivered -- the v1 at-most-once
      failure mode).
    * :attr:`SUCCEEDED` -- the runner reported a successful outcome via
      ``POST .../result``; ``result`` + ``completed_at`` stamped. Terminal.
    * :attr:`FAILED` -- the runner reported a failure; ``error`` +
      ``completed_at`` stamped. Terminal.

    The enum and the ``CHECK (status IN (...))`` constraint on the DB
    table move in lock-step (migration ``0059``); the drift guard
    :func:`tests.migrations.test_migration_0059_create_gateway_command.test_status_check_matches_enum`
    asserts equality at unit-test time.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Closed ``gateway_command.status`` vocabulary derived from the enum --
#: kept in sync with migration ``0059``'s ``_GATEWAY_COMMAND_STATUSES``
#: literal. The drift guard asserts equality so the two never diverge.
_GATEWAY_COMMAND_STATUSES: tuple[str, ...] = tuple(s.value for s in GatewayCommandStatus)


class GatewayCommand(Base):
    """One centrally-enqueued operation queued for a satellite runner.

    Initiative #2415 (Remote execution gateway), Task #2498. Central code
    enqueues a pre-authorized operation (via
    :func:`meho_backplane.gateway.queue.enqueue_command`); the runner
    claims it over the outbound long-poll
    (``GET /api/v1/gateway/{runner}/next``) and reports the outcome back
    (``POST /api/v1/gateway/{runner}/result``). The row is the durable
    transport state that lets a central instance relay an operation to a
    runner it cannot dial directly, without holding the request across a
    process restart. Moulded on the ``approval_request`` durable-queue row
    (#817): closed status enum + DB CHECK + drift guard, real tenant FK,
    caller-owns-commit service functions.

    Capability binding (#2500) layers on top of the #2498 transport row:
    ``params_hash`` / ``expires_at`` / ``consumed_at`` / ``mint_audit_id``
    are added by migration ``0061`` so a delivered command is bound to
    ``(runner, op, target, args-hash, expiry)`` and consumed at most once.
    The row *is* the capability token — an opaque UUID PK, verified by DB
    lookup and revoked/consumed by a conditional UPDATE, never a signed
    stateless artifact (at-most-once inherently needs central state).

    Schema decisions
    ----------------

    * ``id`` -- UUID primary key. PG-side ``gen_random_uuid()``; ORM
      ``default=uuid.uuid4`` for SQLite.

    * ``tenant_id`` -- UUID NOT NULL, real FK to ``tenant.id``. Clean-slate
      table; hard FK enforced (no ondelete). Same discipline as
      ``approval_request`` (0023) and ``runner_principal`` (0058).

    * ``runner_id`` -- Text NOT NULL. The runner principal **name** (the
      wire identity: #2498's ``{runner}`` path segment, ``MEHO_RUNNER_ID``
      on the runner, ``RunnerResultBatch.runner_id`` on the wire). Named
      ``runner_id`` to match that wire field; the guard binds the token's
      ``runner_id`` UUID claim to the named ``runner_principal`` row before
      any queue access, so filtering by name is correctly scoped.

    * ``op_id`` -- Text NOT NULL. The operation the runner executes.

    * ``params`` -- portable JSON NOT NULL DEFAULT ``{}`` (JSONB on PG).
      The validated op params.

    * ``target_descriptor`` -- portable JSON **nullable** (JSONB on PG).
      The centrally-resolved target descriptor a connector handler
      duck-reads (the runner has no local target table). Nullable because
      targetless synthetic ops (``net.*``) carry no descriptor, which the
      wire model encodes as ``RunnerWorkItem.target_descriptor:
      ResolvedTargetDescriptor | None`` (#2497) -- NULL is the
      wire-compatible encoding of "targetless".

    * ``status`` -- Closed enum, DB ``CHECK``, default ``'pending'``.

    * ``result`` -- portable JSON nullable (JSONB on PG). The runner's
      success payload; NULL until reported.

    * ``error`` -- Text nullable. The runner's failure summary; NULL until
      a failure is reported.

    * ``enqueued_by_sub`` -- Text NOT NULL. The ``sub`` of the principal
      whose central dispatch enqueued the command (audit provenance).

    * ``enqueued_at`` -- ``timestamptz`` NOT NULL. Drives the FIFO claim
      order.

    * ``delivered_at`` / ``completed_at`` -- ``timestamptz`` nullable.
      Stamped on the ``pending -> delivered`` claim and the
      ``delivered -> terminal`` report respectively.

    Capability binding (#2500, migration ``0061``)
    ----------------------------------------------

    * ``params_hash`` -- Text NOT NULL. ``compute_params_hash(params)`` at
      mint. The delivery path re-hashes the stored ``params`` against it
      and refuses delivery on mismatch (post-mint substitution defence,
      moulded on ``approve_request``). The migration sentinel default
      ``''`` only satisfies the NOT NULL ADD COLUMN on the empty
      clean-slate table; every real row is stamped by ``enqueue_command``.

    * ``expires_at`` -- ``timestamptz`` NOT NULL. Bounded at mint against a
      module-constant default TTL (caller may only shorten). The claim
      predicate requires ``expires_at > now``, so an expired capability is
      never delivered. The sentinel default (epoch) is fail-closed
      (already expired) for the same ADD COLUMN reason as ``params_hash``.

    * ``consumed_at`` -- ``timestamptz`` nullable one-way latch. Won by a
      single conditional ``UPDATE ... SET consumed_at = now WHERE
      consumed_at IS NULL AND status = 'delivered'`` (``consume_command``,
      moulded on ``claim_resume``): the loser of a replayed result is
      refused (``command_already_consumed``), so a result is accepted at
      most once. A consumed row is also excluded from claiming.

    * ``mint_audit_id`` -- UUID nullable **soft** FK to ``audit_log.id``
      (no DB FK, same discipline as ``audit_log.parent_audit_id``). The id
      of the synchronous ``gateway.command.mint`` audit row; the accepted
      result's audit row stamps ``parent_audit_id = mint_audit_id`` so a
      remote execution forms one audit subtree.

    Index
    -----

    * ``gateway_command_claim_idx`` -- composite ``(tenant_id, runner_id,
      status, enqueued_at)``. Serves the hot claim query (oldest
      ``pending`` row for a runner in a tenant) and the tenant/runner-scoped
      result lookup.
    """

    __tablename__ = "gateway_command"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK -- clean-slate substrate (see class docstring).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # The runner principal NAME (wire identity), not the UUID row id.
    runner_id: Mapped[str] = mapped_column(Text, nullable=False)
    op_id: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    # Nullable -- NULL is the wire-compatible "targetless" encoding.
    target_descriptor: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=GatewayCommandStatus.PENDING.value,
    )
    result: Mapped[dict[str, object] | None] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    enqueued_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # --- Capability binding (#2500, migration 0061) --------------------
    # NOT NULL with a sentinel server_default: the ADD COLUMN lands on the
    # empty clean-slate table across PG + SQLite (SQLite forbids a
    # CURRENT_TIMESTAMP / expression default on ADD COLUMN, so the default
    # is a constant), and both sentinels are fail-closed. ``enqueue_command``
    # stamps the real values on every minted row.
    params_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=sa.text("''"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("'1970-01-01 00:00:00+00:00'"),
    )
    # One-way consumption latch (NULL until the result is accepted once).
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Soft FK to audit_log.id -- the mint audit row's id (mint lineage).
    mint_audit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "gateway_command_claim_idx",
            "tenant_id",
            "runner_id",
            "status",
            "enqueued_at",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            _ck_in("status", _GATEWAY_COMMAND_STATUSES),
            name="ck_gateway_command_status",
        ),
    )


# ---------------------------------------------------------------------------
# Event outbox (G11.3-T3 / #824)
# ---------------------------------------------------------------------------


class EventOutbox(Base):
    """One durable MEHO-internal event ready for subscription dispatch.

    Initiative #804 (G11.3 Scheduler P2), Task #824 (T3). The
    transactional outbox: producers insert one of these rows in the
    same DB transaction that writes the event-producing state change
    (an :class:`AgentRun` transitioning to ``succeeded`` / ``failed`` /
    ``cancelled``; future kinds: audit predicates, connector alerts).
    A separate drain loop (:mod:`meho_backplane.events.drain`) scans the
    outbox via ``SELECT ... FOR UPDATE SKIP LOCKED``, claims unprocessed
    rows, and dispatches them to subscribed
    :class:`ScheduledTrigger` rows of kind ``'event'`` once the
    subscription matcher lands (T5 #826).

    Why a transactional outbox (not raw ``LISTEN/NOTIFY``)
    ------------------------------------------------------

    Plain PG ``LISTEN/NOTIFY`` loses notifications sent while no
    listener is connected. For an event-driven agent trigger that must
    survive process restarts that loss is unacceptable. The
    transactional outbox is the durable, replica-safe alternative; the
    drain loop's ``SELECT ... FOR UPDATE SKIP LOCKED`` makes it
    multi-replica safe (no double-dispatch). ``LISTEN/NOTIFY`` is
    layered on top as a sub-second wake hint; the drain still ticks
    on a 5-10s timer so a dropped notification is benign.

    Append-only discipline
    ----------------------

    Producers only ever ``INSERT`` into this table; the drain loop
    is the only mutator (stamps ``claimed_at`` / ``claimed_by`` and
    eventually ``processed_at``). The :class:`AuditLog` append-only
    recipe (one row per event, indexed by tenant + sequence) shapes
    this table; the model carries no transition helpers because the
    drain is the only mutator and lives in its own service module.

    Schema decisions
    ----------------

    * ``event_id`` -- ``BIGSERIAL`` primary key on PG; ``Integer``
      autoincrement on SQLite via :data:`_PORTABLE_BIG_SERIAL`. Monotonic
      so the drain's "scan unprocessed events" query has a natural
      ordering key without timestamp ties; the drain queries
      ``WHERE processed_at IS NULL ORDER BY event_id``.

    * ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES tenant(id)``
      FK (migration 0027). Same discipline as :attr:`AgentRun.tenant_id`
      / :attr:`ScheduledTrigger.tenant_id`.

    * ``event_kind`` -- Text NOT NULL. The discriminator the matcher
      will use once the subscription-junction lands. Free-text (not a
      closed enum) because event kinds are added per-Initiative
      without coordinated DB migrations; the matching policy lives in
      the subscriber. v0.2 values shipped: ``agent_run.completed``.

    * ``payload`` -- portable JSON -> JSONB NOT NULL DEFAULT ``'{}'``.
      The event-specific payload the subscriber's filter matches
      against. Not-null with a default keeps a payload-less event
      insertable without ambiguity at the SQL layer.

    * ``claimed_at`` / ``claimed_by`` -- ``timestamptz`` + Text, both
      nullable. Stamped by the drain on a successful claim;
      ``claimed_by`` records a process identifier so an operator can
      observe which replica is handling a stuck claim.

    * ``processed_at`` -- ``timestamptz`` nullable. Stamped after the
      event has been dispatched (or marked no-op in v0.2 when no
      subscriber matches). NULL means "not yet processed"; the partial
      index keys on this column.

    * ``created_at`` -- ``timestamptz`` NOT NULL DEFAULT ``now()``.

    Indexes
    -------

    * ``event_outbox_tenant_unprocessed_idx`` -- b-tree on
      ``(tenant_id, processed_at, event_id)``. Drives the future
      tenant-scoped scan once the matcher lands.
    * ``event_outbox_unprocessed_idx`` -- partial b-tree on
      ``event_id`` ``WHERE processed_at IS NULL`` on PG (plain b-tree
      on SQLite). Drives the global drain scan; partial keeps the
      index size flat as processed rows are tombstoned.
    """

    __tablename__ = "event_outbox"

    event_id: Mapped[int] = mapped_column(
        _PORTABLE_BIG_SERIAL,
        primary_key=True,
        autoincrement=True,
    )
    # Real REFERENCES tenant(id) FK -- migration 0027 enforces it at
    # the DB layer (same discipline AgentRun / ScheduledTrigger follow).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL with a default of ``{}`` (see class docstring); the
    # ``none_as_null`` flag stays off because a producer-side ``None``
    # is a bug, not a NULL-storing intent.
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    claimed_by: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "event_outbox_tenant_unprocessed_idx",
            "tenant_id",
            "processed_at",
            "event_id",
            postgresql_using="btree",
        ),
        Index(
            "event_outbox_unprocessed_idx",
            "event_id",
            postgresql_using="btree",
            postgresql_where=sa.text("processed_at IS NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# G11.5-T5 — per-identity token budget model (C3-a)
# ---------------------------------------------------------------------------


class BudgetWindowKind(StrEnum):
    """Closed vocabulary of budget-window granularities.

    Each :class:`IdentityBudget` row is keyed in part on a
    :class:`BudgetWindowKind` — the budget answers "what's the cap for
    this principal *per day / per week / per month*". The runtime
    increments one row per active window-kind on every successful run
    (one daily bucket, one weekly bucket, one monthly bucket).

    The vocabulary is intentionally closed (three values). A fourth
    granularity would require both a code change and an Alembic
    migration (the
    :class:`~meho_backplane.db.models.IdentityBudget.window_kind`
    column carries a DB-level CHECK constraint backed by this enum),
    which is the cheapest way to prevent drift between the DB row, the
    consumption service, and the enforcement gate (G11.5-C3-b, #1080).

    Members:

    * :attr:`DAILY` — buckets start at 00:00 UTC and last 24 hours.
    * :attr:`WEEKLY` — ISO-week buckets, starting Monday 00:00 UTC.
    * :attr:`MONTHLY` — calendar-month buckets, starting the 1st at
      00:00 UTC.
    """

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class IdentityBudget(Base):
    """One per-(tenant, principal, window-kind, window-start) budget bucket.

    Initiative #806 (G11.5 Portability + cost), Task #1079 (G11.5-T5 /
    C3-a). The table is the data substrate for per-identity LLM token /
    cost / request budgets attached to *any* MEHO principal (human,
    service account, or agent — see :class:`AgentPrincipal` for the
    agent-only registry). Each row carries:

    * **Limits** (``token_limit`` / ``cost_limit`` / ``request_limit``)
      — nullable; ``NULL`` means *"no cap on this dimension"*. Limits
      are set by an operator / seed once and are static within a
      window bucket; rotating to the next bucket carries over the
      limits via a service-side helper, not a DB constraint.
    * **Consumption** (``tokens_consumed`` / ``cost_consumed`` /
      ``requests_consumed``) — NOT NULL with default 0; incremented by
      the runtime's
      :func:`~meho_backplane.operations.identity_budget.apply_consumption`
      after every successful agent run. ``cost_consumed`` is
      :class:`~decimal.Decimal` to keep arithmetic precision tight on
      the money-shaped quantity; ``tokens_consumed`` is
      :class:`~decimal.Decimal` rather than ``int`` because some
      providers emit per-window aggregates that exceed 64-bit
      ``Integer`` range when the prompt cache is hot (cache reads
      count as a separate token stream).

    The row is **keyed** by
    ``(tenant_id, principal_sub, window_kind, window_start)`` —
    enforced both at the DB layer (``uq_identity_budget_window``) and
    by every upsert in the consumption service. A duplicate would feed
    nondeterministic increments (*"which bucket do we charge?"*).

    Schema decisions
    ----------------

    * ``id`` — UUID primary key. PG ``gen_random_uuid()`` server
      default via the migration; SQLite via ORM
      ``default=uuid.uuid4``. The unique key is the
      ``(tenant_id, principal_sub, window_kind, window_start)`` tuple
      — the ``id`` exists only to give the row a stable handle for
      cross-table references (none in v0.2).

    * ``tenant_id`` — UUID NOT NULL, ``REFERENCES tenant(id)``. Brand-
      new table, no chassis-era rows — same FK discipline as
      :class:`AgentPermission` (0022).

    * ``principal_sub`` — Text NOT NULL. The JWT ``sub`` claim of the
      principal whose budget this is. Same soft-FK discipline as
      :attr:`AgentPermission.principal_sub`: the principal can be a
      human (no row in any principal table), a service account, or an
      agent, and the JWT ``sub`` is the stable Keycloak-issued
      identifier across all three.

    * ``window_kind`` — Text NOT NULL with a portable
      ``CHECK window_kind IN ('daily', 'weekly', 'monthly')``
      constraint enforcing the closed :class:`BudgetWindowKind`
      vocabulary. The enum and the constraint move in lock-step; a
      drift guard in :mod:`tests.test_db_identity_budget` asserts
      equality.

    * ``window_start`` / ``window_end`` — ``timestamptz`` NOT NULL.
      Inclusive lower / exclusive upper bound of the bucket. The
      consumption service truncates *"now"* to the window-kind
      boundary (00:00 UTC for daily / Monday 00:00 UTC for weekly /
      1st of the month at 00:00 UTC for monthly) at upsert time, so
      every row's pair is canonical and reproducible from
      ``(window_kind, window_start)`` alone. ``window_end`` is
      persisted so an audit / dashboard reader does not have to
      re-derive the boundary.

    * ``token_limit`` — ``Numeric(20, 0)`` nullable. NULL = no cap.
      Wider than ``Integer`` for the same hot-prompt-cache reason
      ``tokens_consumed`` is widened.

    * ``cost_limit`` — ``Numeric(14, 6)`` nullable. NULL = no cap.
      Eight integer digits hold "ten million USD per window"; six
      fractional digits keep micro-cent precision for cache-read
      rates.

    * ``request_limit`` — ``Integer`` nullable. NULL = no cap. The
      request count per principal per window is comfortably bounded
      by a 32-bit signed range (≤2.1B), so a plain :class:`Integer`
      suffices.

    * ``tokens_consumed`` — ``Numeric(20, 0)`` NOT NULL DEFAULT 0.
      Same precision rationale as ``token_limit``.

    * ``cost_consumed`` — ``Numeric(14, 6)`` NOT NULL DEFAULT 0.
      Same precision rationale as ``cost_limit``.

    * ``requests_consumed`` — ``Integer`` NOT NULL DEFAULT 0.

    * ``created_at`` / ``updated_at`` — ``timestamptz`` NOT NULL. PG
      server defaults ``now()``; ORM ``default=lambda:
      datetime.now(UTC)`` for SQLite.

    Indexes / constraints
    ---------------------

    * ``uq_identity_budget_window`` — unique on
      ``(tenant_id, principal_sub, window_kind, window_start)``.
      Drives the upsert path and prevents duplicate buckets.
    * ``ck_identity_budget_window_kind`` — CHECK on ``window_kind``.
    * ``identity_budget_tenant_principal_idx`` — b-tree on
      ``(tenant_id, principal_sub)``. Drives the post-run consumption
      walk (find the active buckets for this principal in this
      tenant) and the dashboard / enforcement read.
    """

    __tablename__ = "identity_budget"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Real FK to tenant.id -- brand-new table, no chassis-era rows.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tenant.id"),
        nullable=False,
    )
    # Soft reference -- principal can be human / service / agent.
    principal_sub: Mapped[str] = mapped_column(Text, nullable=False)
    # Closed CHECK-backed vocabulary (BudgetWindowKind).
    window_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Inclusive lower bound of the bucket; truncated to window boundary.
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Exclusive upper bound; persisted for audit reads.
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Limits: nullable = "no cap on this dimension".
    token_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 0),
        nullable=True,
        default=None,
    )
    cost_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6),
        nullable=True,
        default=None,
    )
    request_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
    )
    # Consumption: NOT NULL DEFAULT 0; incremented by apply_consumption.
    tokens_consumed: Mapped[Decimal] = mapped_column(
        Numeric(20, 0),
        nullable=False,
        default=lambda: Decimal(0),
    )
    cost_consumed: Mapped[Decimal] = mapped_column(
        Numeric(14, 6),
        nullable=False,
        default=lambda: Decimal(0),
    )
    requests_consumed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
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
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "principal_sub",
            "window_kind",
            "window_start",
            name="uq_identity_budget_window",
        ),
        sa.CheckConstraint(
            "window_kind IN ('daily', 'weekly', 'monthly')",
            name="ck_identity_budget_window_kind",
        ),
        Index(
            "identity_budget_tenant_principal_idx",
            "tenant_id",
            "principal_sub",
            postgresql_using="btree",
        ),
    )


# ---------------------------------------------------------------------------
# Runbook schema (G12.1-T1 / #1292)
# ---------------------------------------------------------------------------


class RunbookTemplate(Base):
    """A versioned runbook recipe (immutable on publish).

    One row per ``(tenant_id, slug, version)`` triple. The ``slug`` is
    the operator-facing stable identifier for the procedure (e.g.
    ``drain-k8s-node``); ``version`` is a monotonically increasing
    integer per ``(tenant_id, slug)`` — first draft starts at 1, each
    edit that bumps the draft creates a new version row. The G12.2
    write layer rejects edits to published templates (a new version must
    be created).

    Schema decisions
    ----------------

    * ``id`` — UUID primary key; ``default=uuid.uuid4`` for the SQLite
      dev/test path (no ``gen_random_uuid()`` server default in v0.2).
    * ``tenant_id`` — UUID NOT NULL. Soft-FK per existing discipline
      (no DB-level FK to ``tenant.id`` in v0.2 — same as every other
      per-tenant table that predates the FK-tightening pass).
    * ``slug`` — Text NOT NULL. Validated against
      :data:`meho_backplane.kb.schemas.SLUG_PATTERN` at the schema
      layer (G12.2); the DB only stores the pre-validated string.
    * ``version`` — Integer NOT NULL. Uniqueness enforced by
      ``runbook_templates_tenant_slug_version_idx`` (unique b-tree on
      ``(tenant_id, slug, version)``).
    * ``steps`` — ``_PORTABLE_JSON`` NOT NULL. Ordered list of step
      descriptors; shape validation (discriminated
      ``type: operation_call`` / ``type: manual`` unions) lives in the
      Pydantic layer (G12.2).
    * ``status`` — Text NOT NULL DEFAULT ``'draft'``. Drives the
      lifecycle machine ``draft → published → deprecated``.
      ``CheckConstraint`` enforces the closed vocabulary.
    * ``created_by`` / ``edited_by`` — operator sub (JWT ``sub`` claim).
      ``edited_by`` mirrors ``created_by`` on first creation; updated by
      the G12.2 edit surface on each subsequent write.
    * ``created_at`` / ``edited_at`` — ``timestamptz`` NOT NULL.
      ``default=lambda: datetime.now(UTC)`` so SQLite dev/test paths
      populate the column without relying on a dialect server default.

    Indexes
    -------

    * ``runbook_templates_tenant_slug_version_idx`` — unique b-tree on
      ``(tenant_id, slug, version)``.  Templates are always
      tenant-scoped so a single full unique index suffices (no partial-
      index split like ``operation_group_global_idx`` /
      ``operation_group_tenant_idx``).
    * ``runbook_templates_tenant_status_idx`` — b-tree on
      ``(tenant_id, status)`` for the ``runbook_list_templates`` query
      path (G12.2).
    """

    __tablename__ = "runbook_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Soft-FK — no DB-level FK to tenant.id in v0.2 (same discipline
    # as every other per-tenant table in this module).
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list[dict[str, object]]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=list,
    )
    target_kind: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    edited_by: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "runbook_templates_tenant_slug_version_idx",
            "tenant_id",
            "slug",
            "version",
            unique=True,
            postgresql_using="btree",
        ),
        Index(
            "runbook_templates_tenant_status_idx",
            "tenant_id",
            "status",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'deprecated')",
            name="ck_runbook_templates_status",
        ),
    )


class RunbookRun(Base):
    """An execution of a :class:`RunbookTemplate` — one row per invocation.

    The state machine drives ``in_progress → completed | abandoned``.
    ``template_slug`` and ``template_version`` are pinned at run start so
    later template edits cannot alter an in-flight run's step list. The
    ``params`` column carries the ``${run.params.X}`` substitution context
    (G12.3); it defaults to an empty dict so a params-less run is
    insertable without ambiguity.

    Schema decisions
    ----------------

    * ``run_id`` — UUID primary key; ``default=uuid.uuid4`` for dev/test.
    * ``tenant_id`` — UUID NOT NULL. Soft-FK (no DB-level FK in v0.2).
    * ``template_slug`` / ``template_version`` — pinned at start. Composite
      soft-reference to ``(tenant_id, slug, version)`` in
      ``runbook_templates`` — no DB-level multi-column FK in v0.2 by the
      existing discipline.
    * ``params`` — ``_PORTABLE_JSON`` NOT NULL. ``default=dict`` for the
      SQLite path; PG gets ``'{}'`` as the server default in the migration
      (same pattern as ``event_outbox.payload`` in migration ``0027``).
    * ``state`` — Text NOT NULL DEFAULT ``'in_progress'``.
      ``CheckConstraint`` enforces the closed vocabulary.
    * ``completed_at`` / ``abandoned_at`` — NULL until the respective
      terminal transition.

    Indexes
    -------

    * ``runbook_runs_tenant_assigned_state_idx`` — b-tree on
      ``(tenant_id, assigned_to, state)`` — drives the G12.4 priming
      query ("in-progress runs assigned to this operator") and
      ``runbook_list_runs`` (G12.3).
    * ``runbook_runs_tenant_template_idx`` — b-tree on
      ``(tenant_id, template_slug, template_version)`` — drives the
      G12.3 post-completion read-allowance lookup ("did this operator
      run this template?").
    """

    __tablename__ = "runbook_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Soft-FK (same discipline as tenant_id on other tables).
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    # Pinned at start; composite soft-reference to runbook_templates.
    template_slug: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_to: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(
        _PORTABLE_JSON,
        nullable=False,
        default=dict,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, default="in_progress")
    # External change-ticket reference (work_ref I3-T1 #1661). The opaque
    # cross-system reference (a GitHub issue, a Jira key, a CR id, e.g.
    # ``"gh:evoila/meho#9"``) of the change record this run executes
    # under. Pinned on the run row at start so every per-step
    # ``operation_call`` audit row can inherit it: the engine binds the
    # shared ``work_ref_var`` ContextVar from this column around each
    # step's dispatch (alongside ``run_id``), so the dispatcher's audit
    # writer stamps the same value on each step's ``audit_log.work_ref``
    # (:data:`meho_backplane.operations._audit.work_ref_var`). NULL when
    # the run was started without a change ticket. No FK -- same soft-
    # reference discipline as ``tenant_id`` / ``audit_log.work_ref``.
    # Added by migration ``0040``.
    work_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    started_by: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    abandoned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index(
            "runbook_runs_tenant_assigned_state_idx",
            "tenant_id",
            "assigned_to",
            "state",
            postgresql_using="btree",
        ),
        Index(
            "runbook_runs_tenant_template_idx",
            "tenant_id",
            "template_slug",
            "template_version",
            postgresql_using="btree",
        ),
        Index(
            "runbook_runs_tenant_work_ref_idx",
            "tenant_id",
            "work_ref",
            postgresql_using="btree",
        ),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed', 'abandoned')",
            name="ck_runbook_runs_state",
        ),
    )


class RunbookRunStepState(Base):
    """Per-(run, step) state for a :class:`RunbookRun`.

    One row per step per run; created in bulk when a run is started
    (G12.3), all rows initially in ``state='pending'``. The composite PK
    ``(run_id, step_id)`` is the natural join key; no additional index is
    needed because the PK covers the per-run advance query path (G12.3:
    "advance run X to step Y").

    Schema decisions
    ----------------

    * ``run_id`` — part of the composite PK. Real
      ``ForeignKey("runbook_runs.run_id", ondelete="CASCADE")`` — this
      is a new-table child relationship, so a DB-level FK is
      appropriate (same pattern as :class:`GraphEdge` → :class:`GraphNode`
      with ``ondelete="CASCADE"``). Cascade-delete so dropping a run row
      also removes its step states.
    * ``step_id`` — part of the composite PK. Matches the ``id`` field
      inside ``runbook_templates.steps[]``; validated by the G12.3 layer
      at write time.
    * ``state`` — Text NOT NULL DEFAULT ``'pending'``. Drives the per-step
      state machine. ``CheckConstraint`` enforces the closed vocabulary.
    * ``started_at`` — NULL while ``state='pending'``; stamped when the
      step transitions to ``in_progress``.
    * ``verified_at`` — NULL until the step reaches ``verified``.
    * ``verify_response`` — nullable JSONB. Captures the operator's
      confirmation result (``yes`` / ``no`` / ``escalate`` for ``confirm``
      steps) or the dispatched-call result (for ``operation_call`` steps).
      NULL while the step is in-progress or pending.
    """

    __tablename__ = "runbook_run_step_states"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("runbook_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    verify_response: Mapped[object] = mapped_column(
        _PORTABLE_JSON,
        nullable=True,
        default=None,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('pending', 'in_progress', 'verified', 'failed')",
            name="ck_runbook_run_step_states_state",
        ),
    )

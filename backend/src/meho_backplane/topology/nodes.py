# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-curated ``graph_node`` create-or-get for the topology graph.

Initiative #772 (G0.9.1), Task #778 (T6, Signal #14). The G9.1 refresh
service (:mod:`meho_backplane.topology.refresh`) is the only path that
inserts :class:`~meho_backplane.db.models.GraphNode` rows — it runs from
the CLI verb ``meho topology refresh <target>`` and writes auto-derived
nodes. There is **no MCP entry point** to create a node directly, so a
fresh tenant (zero nodes; no probe has run yet) cannot reach a working
topology state via MCP: ``meho.topology.annotate`` requires both
endpoints to already exist as ``graph_node`` rows and surfaces
``NodeNotFoundError`` ("no graph_node matched name 'rdc-vault' in this
tenant") with no in-tool remediation.

:func:`create_or_get_node` is the manual-seed primitive that closes
the bootstrap gap. It mirrors the
:func:`~meho_backplane.topology.annotate.annotate_edge` shape:

* **Tenant-scoped.** ``operator.tenant_id`` is the boundary; no
  ``tenant_id`` argument so cross-tenant creation is structurally
  impossible.
* **Idempotent on ``(tenant_id, kind, name)``.** The
  ``graph_node_tenant_kind_name_idx`` unique key drives the lookup;
  a repeat call refreshes ``last_seen`` + merges ``properties`` instead
  of erroring with a unique-constraint violation. Manual seeds set
  ``source='curated'`` + ``discovered_by=operator.sub`` (the operator
  is the canonical author); an idempotent re-seed of an
  already-curated row keeps that author, while a re-seed over an
  auto-discovered row promotes it to ``source='curated'`` +
  ``discovered_by=operator.sub`` (same shape as
  :func:`annotate_edge`'s auto→curated promotion; #2536). The
  ``source`` column is what the refresh service keys its curated-node
  durability discipline on.
* **Audit + broadcast.** One ``audit_log`` row (``op_id=
  'topology.create_node'``, ``op_class='write'``) + one broadcast event
  per call. ``op_class`` is set explicitly: ``.create_node`` is not in
  :data:`broadcast.events._WRITE_SUFFIXES` so the classifier would fall
  through to ``other`` (same reason :func:`annotate_edge` sets it
  explicitly).
* **No probe semantics.** This verb is for manual seeds (the
  bootstrap entry point + curated inner-graph nodes like
  ``vault-role``); it does not trigger a refresh, does not set
  ``target_id`` (manual seeds reference targets by separate
  annotation), and does not soft-delete rows it does not touch.

The function is **session-first** like the rest of the topology write
half — the caller passes an :class:`AsyncSession` with no active
transaction and the function opens its own ``session.begin()`` block.
Broadcast publish happens after commit and is fail-open (matches
:func:`annotate_edge`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from meho_backplane.broadcast import BroadcastEvent, publish_event
from meho_backplane.db.models import (
    KIND_SLUG_MAX_LENGTH,
    KIND_SLUG_MIN_LENGTH,
    KIND_SLUG_PATTERN,
    WELL_KNOWN_NODE_KINDS,
    AuditLog,
    GraphHistoryChangeKind,
    GraphNode,
    is_valid_kind_slug,
)
from meho_backplane.topology.history import node_snapshot, record_node_change

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "CreateNodeResult",
    "InvalidNodeKindError",
    "create_or_get_node",
]

_log = structlog.get_logger(__name__)

#: Canonical op-id. Mirrored into ``audit_log.payload['op_id']`` and
#: into the broadcast event's ``op_id`` field; consumed by the
#: ``meho status --watch`` viewer.
_CREATE_NODE_OP_ID = "topology.create_node"

#: ``op_class`` for the verb. Set explicitly rather than derived via
#: :func:`broadcast.events.classify_op` because ``.create_node`` is not
#: in :data:`broadcast.events._WRITE_SUFFIXES` and the classifier would
#: fall through to ``other``. The verb is a tenant-scoped write that
#: must broadcast in full per the G6.1 default classifier — which the
#: ``write`` op-class enables (same rationale as
#: :data:`annotate._OP_CLASS`).
_OP_CLASS = "write"

#: Non-HTTP audit method token (chassis convention: a non-HTTP write
#: records ``method`` as a verb token and ``path`` as the canonical
#: op_id, mirroring :data:`refresh._AUDIT_METHOD` and
#: :data:`annotate._AUDIT_METHOD_ANNOTATE`).
_AUDIT_METHOD = "CREATE_NODE"

#: Property keys that change on every :func:`create_or_get_node` call
#: regardless of operator intent — heartbeats stripped from the
#: meaningful-change comparison so an idempotent re-seed does not emit
#: a phantom ``updated`` history row. ``seeded_at`` is the
#: ``datetime.now(UTC).isoformat()`` stamped into ``properties`` on
#: every call (the create-node side of the same heartbeat pattern
#: :data:`annotate._ANNOTATE_HEARTBEAT_PROPERTY_KEYS` carries for
#: ``annotated_at``). Top-level ``last_seen`` is also a heartbeat and
#: is handled inside :func:`_create_node_is_meaningful` itself.
_CREATE_NODE_HEARTBEAT_PROPERTY_KEYS: tuple[str, ...] = ("seeded_at",)


class InvalidNodeKindError(ValueError):
    """The supplied ``kind`` is not a valid kind slug.

    Raised by :func:`create_or_get_node` before any DB write — the
    slug grammar (T1 #2534's open vocabulary) is the first guard rail
    and failing here avoids a more obscure DB ``CHECK
    ck_graph_node_kind`` violation later. The MCP layer maps it to a
    JSON-RPC ``-32602`` with the pattern and the well-known suggestions
    echoed in the message; the REST layer (when one lands) would map it
    to a 422.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"node kind {kind!r} is not a valid kind slug (pattern "
            f"{KIND_SLUG_PATTERN}, {KIND_SLUG_MIN_LENGTH}-"
            f"{KIND_SLUG_MAX_LENGTH} chars); well-known kinds: "
            f"{sorted(WELL_KNOWN_NODE_KINDS)!r}"
        )


@dataclass(frozen=True, slots=True)
class CreateNodeResult:
    """Outcome of one :func:`create_or_get_node` call.

    ``node`` is the upserted :class:`GraphNode` row (post-commit).
    ``was_created`` is ``True`` when the call inserted a fresh row and
    ``False`` when it merged onto an existing row — same shape as
    :class:`~meho_backplane.topology.annotate.AnnotatePlan.was_created`.
    Callers (CLI, MCP front) surface ``was_created`` so the operator
    sees "created" vs. "already existed; refreshed" as distinct
    outcomes without a separate "exists" probe.
    """

    node: GraphNode
    was_created: bool


def _validate_kind(kind: str) -> str:
    """Validate ``kind`` against the open slug grammar; return the value.

    Mirrors :func:`annotate._validate_kind`'s shape — the slug check
    is the first guard rail, failing here avoids a more obscure DB
    ``CHECK`` violation later. Any slug matching
    :data:`~meho_backplane.db.models.KIND_SLUG_PATTERN` (2-63 chars)
    is accepted (T1 #2534's open vocabulary); membership in
    :data:`~meho_backplane.db.models.WELL_KNOWN_NODE_KINDS` is a
    documentation convention, not a gate.
    """
    if not is_valid_kind_slug(kind):
        raise InvalidNodeKindError(kind)
    return kind


def _build_payload(
    *,
    node: GraphNode,
    note: str | None,
    evidence_url: str | None,
    was_created: bool,
) -> dict[str, Any]:
    """Build the shared audit / broadcast payload.

    The same dict lands in ``audit_log.payload`` (full row) and in the
    broadcast event (``op_class='write'`` defaults to full detail). The
    ``was_created`` flag is the "did we insert or merge?" diagnostic
    the audit trail needs to distinguish first-time seeds from
    idempotent re-seeds without joining against ``first_seen``.
    """
    return {
        "op_id": _CREATE_NODE_OP_ID,
        "op_class": _OP_CLASS,
        "node_id": str(node.id),
        "kind": node.kind,
        "name": node.name,
        "was_created": was_created,
        "note": note,
        "evidence_url": evidence_url,
    }


def _build_audit_row(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    target_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> AuditLog:
    """Construct one ``audit_log`` row for a create_node call.

    Mirrors the columns :func:`annotate._build_audit_row` writes
    (status 200, ``method`` as verb token, ``path`` as op_id) and
    pre-allocates ``audit_id`` so the broadcast event's ``audit_id``
    field references the *same* row (the chassis "audit-id
    pre-allocation" pattern).
    """
    return AuditLog(
        id=audit_id,
        occurred_at=datetime.now(UTC),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        target_id=target_id,
        method=_AUDIT_METHOD,
        path=_CREATE_NODE_OP_ID,
        status_code=200,
        request_id=None,
        duration_ms=Decimal("0.00"),
        payload=payload,
    )


def _create_node_is_meaningful(*, before: dict[str, Any] | None, after: dict[str, Any]) -> bool:
    """Return ``True`` when the re-seed's snapshot reflects a real change.

    Compares ``before`` to ``after`` after stripping the heartbeat
    fields (top-level ``last_seen`` and
    ``properties.seeded_at``) that change on every call regardless of
    operator intent. Mirrors :func:`annotate._annotate_curated_is_meaningful`
    and :func:`refresh._properties_differ` so all three history-emission
    paths agree on what counts as a mutation — an idempotent re-seed
    with the same ``(note, evidence_url)`` and no auto→curated
    promotion is a no-op for history purposes.

    ``before`` is ``None`` on a fresh insert; that path is always
    meaningful (the row did not exist), so the caller treats ``None``
    as the "always emit" case before calling this helper.
    """
    if before is None:
        return True

    def _strip(snapshot: dict[str, Any]) -> dict[str, Any]:
        stripped = {k: v for k, v in snapshot.items() if k != "last_seen"}
        props = stripped.get("properties")
        if isinstance(props, dict):
            stripped["properties"] = {
                k: v for k, v in props.items() if k not in _CREATE_NODE_HEARTBEAT_PROPERTY_KEYS
            }
        return stripped

    return _strip(before) != _strip(after)


async def _publish(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    node_name: str,
    payload: dict[str, Any],
) -> None:
    """Fail-open broadcast publish.

    Same shape as :func:`annotate._publish` — emits the broadcast event
    and swallows publisher failure so a broken stream never rolls back
    a successful create_or_get.
    """
    try:
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            principal_name=operator.name,
            target_name=node_name,
            op_id=_CREATE_NODE_OP_ID,
            op_class=_OP_CLASS,
            result_status="ok",
            audit_id=audit_id,
            payload=payload,
        )
        await publish_event(event)
    except Exception:
        _log.exception(
            "topology_create_node_broadcast_failed",
            op_id=_CREATE_NODE_OP_ID,
            tenant_id=str(operator.tenant_id),
        )


# code-quality-allow: pre-existing >100-line function (210 lines on
# main, ~140 of them contract docstring); #2536 adds only the
# source='curated' stamp + promotion flip.
async def create_or_get_node(
    session: AsyncSession,
    operator: Operator,
    *,
    kind: str,
    name: str,
    note: str | None = None,
    evidence_url: str | None = None,
) -> CreateNodeResult:
    """Create or refresh a manual ``graph_node`` row in the operator's tenant.

    The bootstrap-and-curate verb the MCP empty-tenant onboarding path
    needs: an agent can seed nodes (``vault-role``, ``service``,
    ``vm``...) without shelling into the CLI, then call
    :func:`~meho_backplane.topology.annotate.annotate_edge` to assert
    the cross-system edge between them.

    Validates ``kind`` against the open slug grammar
    (:data:`~meho_backplane.db.models.KIND_SLUG_PATTERN`) *before* any
    DB write — raises :class:`InvalidNodeKindError` so a malformed
    kind never reaches the DB-layer CHECK.

    Idempotency keyed on the unique
    ``graph_node_tenant_kind_name_idx`` (``(tenant_id, kind, name)``):

    * **Absent row** → insert with ``source='curated'`` +
      ``discovered_by=operator.sub``
      (operator is the canonical author for manual seeds),
      ``target_id=None`` (manual seeds reference targets by separate
      annotation; the refresh service adopts only auto nodes onto a
      target), ``properties = {note, evidence_url,
      seeded_by, seeded_at}``, ``first_seen = last_seen = now``.
    * **Existing row** → merge the four manual-seed property keys
      (``note``, ``evidence_url``, ``seeded_by``, ``seeded_at``) onto
      the existing ``properties`` JSONB (auto-discovered keys like
      ``status``, ``phase`` are preserved), refresh ``last_seen``,
      promote to ``source='curated'`` + ``discovered_by=operator.sub``
      iff the existing row was probe-derived (mirrors the
      :func:`annotate_edge` auto→curated promotion; #2536). Returns
      ``was_created=False``.

    Writes one ``audit_log`` row (``op_id='topology.create_node'``,
    ``op_class='write'``, ``method='CREATE_NODE'``) in the same
    transaction. Publishes one :class:`BroadcastEvent` after commit
    (fail-open per the refresh / annotate pattern).

    **Diff-on-write hook (G9.3-T2 #857).** A meaningful call adds one
    :class:`GraphNodeHistory` row in the same transaction so
    :func:`query_history` ``kind=history``  / ``kind=timeline`` reflect
    manual seeds the same way they reflect auto-refresh changes (RDC
    #789 finding F-A — manual seeds were previously invisible to the
    history/timeline verbs because only :mod:`refresh` populated the
    history tables). Fresh inserts emit a ``created`` row;
    auto→curated promotions and property changes emit ``updated``; an
    idempotent re-seed with the same ``(note, evidence_url)`` and no
    promotion is a heartbeat-only mutation and emits no history row
    (mirrors :func:`refresh._update_existing_node`'s
    ``is_meaningful_update`` skip and
    :func:`annotate._annotate_curated_is_meaningful`'s heartbeat
    strip).

    Args:
        session: Caller-owned :class:`AsyncSession` with **no active
            transaction**. The function opens its own
            ``session.begin()`` so the upsert + audit write commit or
            roll back together (matches
            :func:`annotate_edge`'s discipline).
        operator: The acting identity. Supplies the tenant scope and
            audit attribution. Role gating (``tenant_admin``) is the
            front layer's job (MCP / REST); the service trusts its
            caller.
        kind: Any slug matching
            :data:`~meho_backplane.db.models.KIND_SLUG_PATTERN`
            (2-63 chars); prefer a
            :data:`~meho_backplane.db.models.WELL_KNOWN_NODE_KINDS`
            member when one fits. A malformed slug raises
            :class:`InvalidNodeKindError`.
        name: ``graph_node.name``. Must be non-empty (the MCP
            inputSchema enforces ``minLength=1`` before the call).
        note: Optional free-text annotation. Stored on
            ``node.properties['note']`` (manual-seed key).
        evidence_url: Optional URL the operator attached as evidence.
            Stored on ``node.properties['evidence_url']`` (manual-seed
            key).

    Returns:
        :class:`CreateNodeResult` with the upserted :class:`GraphNode`
        row + ``was_created`` flag.

    Raises:
        InvalidNodeKindError: ``kind`` is not a valid kind slug.
    """
    canonical_kind = _validate_kind(kind)
    # Pre-allocate ``audit_id`` so the history row's ``audit_id``
    # references the same ``audit_log`` row this call writes (the
    # chassis "audit-id pre-allocation" pattern shared with refresh /
    # annotate). Generating it inside the ``session.begin()`` block
    # would force the history hook to either re-read the audit row
    # or carry a second id — both worse than threading the same
    # uuid through.
    audit_id = uuid.uuid4()

    async with session.begin():
        existing_stmt = select(GraphNode).where(
            GraphNode.tenant_id == operator.tenant_id,
            GraphNode.kind == canonical_kind,
            GraphNode.name == name,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()

        now = datetime.now(UTC)
        seed_props: dict[str, Any] = {
            "note": note,
            "evidence_url": evidence_url,
            "seeded_by": operator.sub,
            "seeded_at": now.isoformat(),
        }

        if existing is None:
            node = GraphNode(
                id=uuid.uuid4(),
                tenant_id=operator.tenant_id,
                kind=canonical_kind,
                name=name,
                target_id=None,
                source="curated",
                properties=dict(seed_props),
                discovered_by=operator.sub,
                first_seen=now,
                last_seen=now,
            )
            session.add(node)
            await session.flush()
            was_created = True
            node_before: dict[str, Any] | None = None
        else:
            # Capture the pre-mutation snapshot **before** rewriting
            # ``properties`` / ``discovered_by`` / ``last_seen`` so
            # the history row's ``before`` reflects the state the
            # operator would have seen on a ``list-nodes``
            # immediately prior to the re-seed (same discipline
            # :func:`refresh._update_existing_node` and
            # :func:`annotate._reannotate_existing_edge` use).
            node_before = node_snapshot(existing)
            # Merge the manual-seed keys onto the existing properties
            # dict. Reassign rather than mutate so SQLAlchemy's JSONB
            # change-detection picks up the write (same reassign
            # discipline ``annotate._append_conflict_marker`` uses).
            merged = dict(existing.properties or {})
            for key, value in seed_props.items():
                merged[key] = value
            existing.properties = merged
            existing.last_seen = now
            # Promote the row to ``source='curated'`` iff it was
            # probe-derived, and credit the operator as the canonical
            # author — same shape as :func:`annotate_edge`'s
            # auto→curated promotion (#2536). From this call onward
            # the refresh service treats the node as operator-owned:
            # a probe re-observation bumps ``last_seen`` only (no
            # property overwrite, no ``target_id`` adoption) and no
            # refresh soft-deletes it. The promoted node is still
            # recognized by future refresh cycles (refresh keys on
            # ``(tenant_id, kind, name)``, not on ``source``).
            if existing.source != "curated":
                existing.source = "curated"
                existing.discovered_by = operator.sub
            node = existing
            was_created = False

        # Diff-on-write hook (G9.3-T2 #857). Emit exactly one
        # ``graph_node_history`` row per meaningful call so
        # ``query_topology kind=history|timeline`` reflect manual
        # seeds the same way they reflect auto-refresh changes (RDC
        # #789 F-A). Idempotent re-seeds whose only change is the
        # heartbeat ``seeded_at`` / ``last_seen`` skip emission — the
        # "no double-write" criterion on the get path.
        node_after = node_snapshot(node)
        if _create_node_is_meaningful(before=node_before, after=node_after):
            record_node_change(
                session,
                node_id=node.id,
                tenant_id=operator.tenant_id,
                change_kind=(
                    GraphHistoryChangeKind.CREATED
                    if was_created
                    else GraphHistoryChangeKind.UPDATED
                ),
                before=node_before,
                after=node_after,
                audit_id=audit_id,
                valid_from=now,
            )

        payload = _build_payload(
            node=node,
            note=note,
            evidence_url=evidence_url,
            was_created=was_created,
        )
        session.add(
            _build_audit_row(
                audit_id=audit_id,
                operator=operator,
                target_id=node.target_id,
                payload=payload,
            )
        )

    await _publish(
        audit_id=audit_id,
        operator=operator,
        node_name=node.name,
        payload=payload,
    )
    return CreateNodeResult(node=node, was_created=was_created)

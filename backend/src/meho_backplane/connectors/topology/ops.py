# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``topology.*`` curated-graph write ops + registrar.

Task #2537 (Initiative #2533); ``.delete_node`` added by #2485
(Initiative #2494). The five curated-graph writes are
registered under the natural key ``(product="topology", version="1.x",
impl_id="topology-graph")``, so the wire ``connector_id`` is
``topology-graph-1.x`` — which round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` back to
``("topology", "1.x", "topology-graph")`` (digit-led version segment,
product = the head's first hyphen segment; the ``secret-broker-1.x``
mold, #1577).

The op ids are the exact strings the topology services already stamp on
their audit rows (``audit_log.path``), so the dispatcher's own
``method="DISPATCH"`` row and the service's domain row correlate on the
same ``path`` value.

Approval posture — the load-bearing dial (#2537):

* ``safety_level="caution"`` + ``requires_approval=False`` parks **agent
  principals only**: the AGENT verdict floor
  (``_SAFETY_DEFAULT["caution"]`` → ``needs-approval`` in
  :mod:`meho_backplane.auth.permissions`) routes an agent write to the
  durable :class:`~meho_backplane.db.models.ApprovalRequest` queue, while
  a human / service principal rides the default-allow branch in
  :func:`~meho_backplane.operations._validate.policy_gate` and executes
  immediately — today's zero-friction tenant_admin UX, unchanged.
* NOT ``requires_approval=True`` — that would park humans too (G11.7-T1
  #1401), which the task explicitly rejects.
* NOT ``safety_level="dangerous"`` — that would DENY agents by default
  rather than park them, defeating the propose-then-approve loop. Graph
  writes are reversible (unannotate + §6 arbitration + append-only
  history), unlike credential moves.

The handlers are module-level functions (no connector instance to bind),
so the dispatcher resolves them with ``connector_instance=None`` and
``target=None``. Each unwraps the validated params and calls the
existing service primitive (:func:`~meho_backplane.topology.annotate.annotate_edge`,
:func:`~meho_backplane.topology.nodes.create_or_get_node`,
:func:`~meho_backplane.topology.annotate.unannotate_edge`) unchanged —
the service owns resolve / validate / upsert / §6 conflict scan / audit
/ broadcast. Domain errors propagate; the dispatcher wraps them as
``connector_error`` results carrying ``extras["exception_class"]`` and
the MCP front maps the known classes back to ``-32602``.

Approve-time re-dispatch is safe: ``annotate_edge`` /
``create_or_get_node`` are idempotent upserts, and the
``resumed_at`` exactly-one-resumer latch (#2293) already guarantees
at-most-once resume.

The JSON Schema documents live in
:mod:`meho_backplane.connectors.topology.schemas` (shared with the MCP
tools' ``inputSchema`` so the two validation layers cannot drift).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.topology.schemas import (
    ANNOTATE_PARAMETER_SCHEMA,
    ANNOTATE_RESPONSE_SCHEMA,
    BULK_IMPORT_PARAMETER_SCHEMA,
    BULK_IMPORT_RESPONSE_SCHEMA,
    CREATE_NODE_PARAMETER_SCHEMA,
    CREATE_NODE_RESPONSE_SCHEMA,
    DELETE_NODE_PARAMETER_SCHEMA,
    DELETE_NODE_RESPONSE_SCHEMA,
    UNANNOTATE_PARAMETER_SCHEMA,
    UNANNOTATE_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.topology.annotate import NodeRef, annotate_edge_with_plan, unannotate_edge
from meho_backplane.topology.bulk_import import (
    build_bulk_import_rows,
    bulk_import_edges,
    serialize_bulk_result,
)
from meho_backplane.topology.node_delete import delete_node
from meho_backplane.topology.nodes import create_or_get_node

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TOPOLOGY_ANNOTATE_OP_ID",
    "TOPOLOGY_BULK_IMPORT_OP_ID",
    "TOPOLOGY_CREATE_NODE_OP_ID",
    "TOPOLOGY_DELETE_NODE_OP_ID",
    "TOPOLOGY_GRAPH_CONNECTOR_ID",
    "TOPOLOGY_UNANNOTATE_OP_ID",
    "register_topology_graph_operations",
    "topology_annotate",
    "topology_bulk_import",
    "topology_create_node",
    "topology_delete_node",
    "topology_unannotate",
]

#: Wire connector_id for the synthetic topology-graph product. Must stay
#: parser-compatible: digit-led version suffix, product recoverable as
#: the head's first hyphen segment (see module docstring).
TOPOLOGY_GRAPH_CONNECTOR_ID = "topology-graph-1.x"

#: Op ids — identical to the ``audit_log.path`` strings the topology
#: services already write (``topology/annotate.py`` / ``topology/nodes.py``).
TOPOLOGY_ANNOTATE_OP_ID = "topology.annotate"
TOPOLOGY_CREATE_NODE_OP_ID = "topology.create_node"
TOPOLOGY_DELETE_NODE_OP_ID = "topology.delete_node"
TOPOLOGY_UNANNOTATE_OP_ID = "topology.unannotate"
TOPOLOGY_BULK_IMPORT_OP_ID = "topology.bulk_import"

_GROUP_KEY = "graph"
_GROUP_WHEN_TO_USE = (
    "Curated writes to the tenant topology graph: seed a graph_node the "
    "probes cannot derive, delete a manually-seeded graph_node, assert a "
    "curated graph_edge between existing nodes, or revoke a curated edge. "
    "Use when cross-system structure "
    "auto-discovery cannot infer needs recording. Agent-principal calls "
    "park as approval requests for a human operator to approve; human "
    "tenant_admin calls execute immediately. Graph reads live on "
    "query_topology, not here."
)


# ---------------------------------------------------------------------------
# Handlers — thin param shims over the service primitives
# ---------------------------------------------------------------------------


async def topology_annotate(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Assert a curated ``graph_edge`` between two existing nodes.

    Op-id: ``topology.annotate``. Targetless typed op. The dispatcher has
    already validated *params* against
    :data:`~meho_backplane.connectors.topology.schemas.ANNOTATE_PARAMETER_SCHEMA`.
    The service primitive owns resolve / validate / upsert / §6 conflict
    scan / audit / broadcast; this shim only builds the two
    :class:`~meho_backplane.topology.annotate.NodeRef` objects and maps
    the edge back to the human-readable response shape. Domain errors
    (:class:`~meho_backplane.topology.query.AmbiguousNodeError`,
    :class:`~meho_backplane.topology.resolvers.NodeNotFoundError`,
    :class:`~meho_backplane.topology.annotate.InvalidEdgeKindError`)
    propagate to the dispatcher's ``connector_error`` envelope.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        plan = await annotate_edge_with_plan(
            session,
            operator,
            NodeRef(str(params["from_name"]), params.get("from_node_kind")),
            str(params["kind"]),
            NodeRef(str(params["to_name"]), params.get("to_node_kind")),
            note=params.get("note"),
            evidence_url=params.get("evidence_url"),
        )
        edge = plan.edge
        # Re-load the endpoint nodes for the response shape. The service
        # returns the edge only; mapping back to the human-readable
        # `(kind, name)` pair is this shim's job.
        from_node = await session.get(GraphNode, edge.from_node_id)
        to_node = await session.get(GraphNode, edge.to_node_id)

    if from_node is None or to_node is None:
        # Endpoint resolution succeeded inside the service transaction
        # but the post-commit reload missed — graph in inconsistent
        # state. ValueError keeps the MCP front's -32602 translation
        # (the audit / broadcast emitted inside annotate_edge is
        # already committed).
        raise ValueError(f"annotated edge {edge.id} endpoint lookup failed post-commit")

    props = edge.properties or {}
    raw_conflicts = props.get("conflicts_with")
    conflicts = list(raw_conflicts) if isinstance(raw_conflicts, list) else []
    # `superseded` (#2539): the auto edges this assertion displaced.
    # Already computed inside the write and stamped on the shared audit /
    # broadcast payload (`annotate._audit_payload`) — surfacing it on the
    # return is a shape change, not a new query. It equals the audit
    # payload's list by construction.
    superseded = [str(s) for s in plan.audit_payload.get("superseded", [])]
    return {
        "edge_id": str(edge.id),
        "from": {
            "id": str(from_node.id),
            "kind": from_node.kind,
            "name": from_node.name,
        },
        "to": {
            "id": str(to_node.id),
            "kind": to_node.kind,
            "name": to_node.name,
        },
        "kind": edge.kind,
        "source": edge.source,
        "conflicts": conflicts,
        "superseded": superseded,
    }


async def topology_create_node(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Manually seed a ``graph_node`` row in the operator's tenant.

    Op-id: ``topology.create_node``. Targetless typed op. Tenant scope
    comes from *operator* inside the service — never from *params*
    (``additionalProperties: false`` already rejects a smuggled
    ``tenant_id`` at the schema layer).
    :class:`~meho_backplane.topology.nodes.InvalidNodeKindError` is
    structurally unreachable (``kind`` is pattern-pinned by the schema)
    but propagates to ``connector_error`` if schema and service grammar
    ever drift.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await create_or_get_node(
            session,
            operator,
            kind=str(params["kind"]),
            name=str(params["name"]),
            note=params.get("note"),
            evidence_url=params.get("evidence_url"),
        )
    return {
        "node_id": str(result.node.id),
        "kind": result.node.kind,
        "name": result.node.name,
        "source": result.node.source,
        "was_created": result.was_created,
    }


async def topology_delete_node(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Guarded hard-delete of a manually-seeded ``graph_node`` row.

    Op-id: ``topology.delete_node``. Targetless typed op. Tenant scope
    comes from *operator* inside the service — never from *params*
    (``additionalProperties: false`` already rejects a smuggled
    ``tenant_id`` at the schema layer). A malformed ``node_id`` raises
    :class:`ValueError` so the MCP front keeps its -32602 translation;
    the service-layer guard errors
    (:class:`~meho_backplane.topology.node_delete.NodeNotFoundForDeleteError`,
    :class:`~meho_backplane.topology.node_delete.NodeNotDeletableError`,
    :class:`~meho_backplane.topology.node_delete.NodeHasLiveEdgesError`)
    propagate to the dispatcher's ``connector_error`` envelope.
    """
    node_id_arg = params["node_id"]
    try:
        node_uuid = uuid.UUID(str(node_id_arg))
    except ValueError as exc:
        raise ValueError(
            f"topology.delete_node: node_id is not a valid UUID: {node_id_arg!r}",
        ) from exc

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await delete_node(session, operator, node_id=node_uuid)
    return {
        "node_id": str(result.node_id),
        "kind": result.kind,
        "name": result.name,
    }


async def topology_unannotate(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Hard-delete a curated ``graph_edge`` and clear its §6 markers.

    Op-id: ``topology.unannotate``. Targetless typed op. The two selector
    forms (UUID primary key vs. ``(from, kind, to)`` triple) are mutually
    exclusive at the schema layer; the service-level
    :class:`~meho_backplane.topology.annotate.UnannotateSelectorError`
    guard stays for never-validated in-process callers. A malformed UUID
    raises :class:`ValueError` so the MCP front keeps its -32602
    translation.
    """
    edge_id_arg = params.get("edge_id")
    edge_uuid: uuid.UUID | None = None
    if edge_id_arg is not None:
        try:
            edge_uuid = uuid.UUID(str(edge_id_arg))
        except ValueError as exc:
            raise ValueError(
                f"topology.unannotate: edge_id is not a valid UUID: {edge_id_arg!r}",
            ) from exc

    from_name = params.get("from_name")
    to_name = params.get("to_name")
    from_ref = NodeRef(str(from_name), params.get("from_node_kind")) if from_name else None
    to_ref = NodeRef(str(to_name), params.get("to_node_kind")) if to_name else None

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        removed_id = await unannotate_edge(
            session,
            operator,
            edge_id=edge_uuid,
            from_ref=from_ref,
            kind=params.get("kind"),
            to_ref=to_ref,
        )
    return {"edge_id": str(removed_id)}


async def topology_bulk_import(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Apply a batch of curated ``graph_edge`` assertions atomically.

    Op-id: ``topology.bulk_import``. Targetless typed op — the **apply**
    half of ``meho.topology.bulk_import`` (#2539). Only the gated apply
    path dispatches here; the free dry-run plan calls the service
    directly from the MCP front and never reaches the dispatcher. The
    dispatcher has already validated *params* against
    :data:`~meho_backplane.connectors.topology.schemas.BULK_IMPORT_PARAMETER_SCHEMA`
    (``rows`` only — no ``dry_run``), so this handler always applies.

    The whole batch lives in one transaction: a validation failure on
    any row rolls the whole thing back
    (:class:`~meho_backplane.topology.bulk_import.BulkImportValidationError`
    propagates to the dispatcher's ``connector_error`` envelope, which
    the MCP shim maps to -32602). Per-row audit + broadcast fire one per
    applied row inside the service, mirroring the single-edge annotate.
    """
    rows = build_bulk_import_rows(params["rows"])
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await bulk_import_edges(session, operator, rows, dry_run=False)
    return serialize_bulk_result(result)


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------

#: Per-op registration payloads. The shared coordinates (product /
#: version / impl_id / group / safety posture) are applied by the
#: registrar loop below so the agents-park / humans-immediate dial is
#: set in exactly one place.
_OPERATION_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": TOPOLOGY_ANNOTATE_OP_ID,
        "handler": topology_annotate,
        "summary": "Assert a curated graph_edge between two existing graph nodes.",
        "description": (
            "Asserts a curated `graph_edge` that auto-discovery cannot "
            "infer (e.g. `k8s-sa-foo` authenticates-via `vault-role-bar`). "
            "Idempotent on the `(from_name, kind, to_name)` triple. Both "
            "endpoints must already exist as `graph_node` rows in the "
            "tenant — seed them with `topology.create_node` first. "
            "Tenant-scoped automatically. Agent-principal calls park as "
            "approval requests; human tenant_admin calls execute "
            "immediately."
        ),
        "parameter_schema": ANNOTATE_PARAMETER_SCHEMA,
        "response_schema": ANNOTATE_RESPONSE_SCHEMA,
        "tags": ["topology", "write", "curated-edge"],
        "llm_instructions": {
            "when_to_use": (
                "Record a cross-system relationship the probes cannot "
                "see. Requires both endpoint nodes to exist; on a fresh "
                "tenant call topology.create_node first."
            ),
            "parameter_hints": {
                "from_name": "Required. `graph_node.name` of the edge's from endpoint.",
                "kind": "Required. Edge kind slug, e.g. 'depends-on'.",
                "to_name": "Required. `graph_node.name` of the edge's to endpoint.",
            },
            "output_shape": (
                "{'edge_id', 'from': {id, kind, name}, 'to': {id, kind, "
                "name}, 'kind', 'source': 'curated', 'conflicts': [...]}"
            ),
        },
    },
    {
        "op_id": TOPOLOGY_CREATE_NODE_OP_ID,
        "handler": topology_create_node,
        "summary": "Manually seed a graph_node row (bootstrap / inner-graph kinds).",
        "description": (
            "Seeds a `graph_node` row the probes cannot derive "
            "(vault-role, keycloak-realm, principal, novel kinds like "
            "dns-record) so a subsequent `topology.annotate` can "
            "reference it. Idempotent on the `(tenant, kind, name)` "
            "triple. Tenant-scoped automatically. Agent-principal calls "
            "park as approval requests; human tenant_admin calls execute "
            "immediately."
        ),
        "parameter_schema": CREATE_NODE_PARAMETER_SCHEMA,
        "response_schema": CREATE_NODE_RESPONSE_SCHEMA,
        "tags": ["topology", "write", "node-seed"],
        "llm_instructions": {
            "when_to_use": (
                "Bootstrap a fresh tenant's graph or seed inner-graph "
                "nodes auto-discovery cannot infer, before annotating "
                "edges against them."
            ),
            "parameter_hints": {
                "kind": "Required. Node kind slug, e.g. 'vault-role' or 'dns-record'.",
                "name": "Required. Unique within (tenant, kind, name).",
            },
            "output_shape": (
                "{'node_id', 'kind', 'name', 'source': 'curated', 'was_created': bool}"
            ),
        },
    },
    {
        "op_id": TOPOLOGY_DELETE_NODE_OP_ID,
        "handler": topology_delete_node,
        "summary": "Hard-delete a manually-seeded graph_node row by id.",
        "description": (
            "Hard-deletes a manually-seeded `graph_node` by `node_id`, "
            "writing a `removed` history tombstone. Refuses probe-owned "
            "nodes (`source='auto'` or bound to a target — they resurrect "
            "on the next refresh) and nodes that still have live edges "
            "(unannotate those first). Only `source='curated'`, "
            "target-unbound seeds are deletable. Tenant-scoped "
            "automatically. Agent-principal calls park as approval "
            "requests; human tenant_admin calls execute immediately."
        ),
        "parameter_schema": DELETE_NODE_PARAMETER_SCHEMA,
        "response_schema": DELETE_NODE_RESPONSE_SCHEMA,
        "tags": ["topology", "write", "node-seed"],
        "llm_instructions": {
            "when_to_use": (
                "Remove a mis-seeded or stale manual node (e.g. a probe-"
                "residue node you seeded by hand). Only manually-seeded "
                "curated nodes with no live edges are deletable; probe-"
                "derived nodes are owned by refresh reconciliation."
            ),
            "parameter_hints": {
                "node_id": (
                    "Required. UUID of the graph_node to delete; from "
                    "query_topology or the create_node response."
                ),
            },
            "output_shape": "{'node_id', 'kind', 'name'}",
        },
    },
    {
        "op_id": TOPOLOGY_UNANNOTATE_OP_ID,
        "handler": topology_unannotate,
        "summary": "Remove a curated graph_edge and clear its reciprocal §6 markers.",
        "description": (
            "Hard-deletes a curated `graph_edge` by `edge_id` or by the "
            "full `(from_name, kind, to_name)` triple (exactly one "
            "selector form), re-promoting any auto edge it had marked "
            "superseded. Refuses `source='auto'` edges — those resurrect "
            "on the next refresh. Tenant-scoped automatically. "
            "Agent-principal calls park as approval requests; human "
            "tenant_admin calls execute immediately."
        ),
        "parameter_schema": UNANNOTATE_PARAMETER_SCHEMA,
        "response_schema": UNANNOTATE_RESPONSE_SCHEMA,
        "tags": ["topology", "write", "curated-edge"],
        "llm_instructions": {
            "when_to_use": (
                "Revoke a wrong or stale curated edge assertion. Pass "
                "either edge_id or the full (from_name, kind, to_name) "
                "triple — never both."
            ),
            "parameter_hints": {
                "edge_id": "UUID selector; mutually exclusive with the triple.",
                "from_name": "Triple selector, with kind + to_name.",
            },
            "output_shape": "{'edge_id': '<removed-uuid>'}",
        },
    },
    {
        "op_id": TOPOLOGY_BULK_IMPORT_OP_ID,
        "handler": topology_bulk_import,
        "summary": "Apply a batch of curated graph_edge assertions atomically.",
        "description": (
            "Applies up to 1000 curated `graph_edge` assertions in one "
            "all-or-nothing transaction — a single invalid row rolls the "
            "whole batch back. This is the apply half of the "
            "`meho.topology.bulk_import` tool; the free dry-run plan is a "
            "read-shaped service call that never dispatches here. Each "
            "row is one `topology.annotate` (both endpoints must already "
            "exist). Tenant-scoped automatically. Agent-principal calls "
            "park the whole batch as one approval request; human "
            "tenant_admin calls execute immediately."
        ),
        "parameter_schema": BULK_IMPORT_PARAMETER_SCHEMA,
        "response_schema": BULK_IMPORT_RESPONSE_SCHEMA,
        "tags": ["topology", "write", "curated-edge", "bulk"],
        "llm_instructions": {
            "when_to_use": (
                "Seed a whole cross-system inventory in one atomic pass "
                "instead of looping single annotate calls. Dry-run first "
                "to see the per-row plan, then apply."
            ),
            "parameter_hints": {
                "rows": (
                    "Required. Array of {from_name, kind, to_name, "
                    "from_node_kind?, to_node_kind?, note?, evidence_url?} "
                    "objects; 1-1000 rows."
                ),
            },
            "output_shape": (
                "{'dry_run', 'created', 'updated', 'conflicts', 'rows': "
                "[{index, action, edge_id, from_name, from_kind, to_name, "
                "to_kind, kind, superseded, conflicts}]}"
            ),
        },
    },
)


async def register_topology_graph_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the five ``topology.*`` typed ops into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. The
    ``embedding_service`` kwarg is the test seam every connector
    registrar carries.

    ``safety_level="caution"`` + ``requires_approval=False`` is the
    agents-park / humans-immediate combination — see the module
    docstring for why neither ``requires_approval=True`` nor
    ``safety_level="dangerous"`` fits.
    """
    for spec in _OPERATION_SPECS:
        await register_typed_operation(
            product="topology",
            version="1.x",
            impl_id="topology-graph",
            group_key=_GROUP_KEY,
            when_to_use=_GROUP_WHEN_TO_USE,
            safety_level="caution",
            requires_approval=False,
            embedding_service=embedding_service,
            **spec,
        )

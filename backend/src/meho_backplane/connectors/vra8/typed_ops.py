# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`Vra8Connector` (#3058).

The vRealize Automation 8.x migration-inventory read core is registered as typed
ops (``source_kind="typed"``) so it dispatches on a fresh boot with **zero catalog
ingest** — the read core is live the moment the connector loads (part of
initiative `evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_,
legacy migration-source connector coverage; this is the vRA 8 legacy dual-impl of
``product="vcfa"``).

The op bodies live in :mod:`meho_backplane.connectors.vra8.typed_reads`; the per-op
``llm_instructions`` in :mod:`meho_backplane.connectors.vra8._llm_instructions`;
thin bound-method shims on
:class:`~meho_backplane.connectors.vra8.connector.Vra8Connector` expose them under
the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk recovers
each callable from its persisted ``module.ClassName.method`` path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.vcd.typed_ops` (#3057) and
:mod:`meho_backplane.connectors.fleet_lcm.typed_ops` (#3047) — the Task #2358
convention: typed ops own the curated read core. A write surface (create
deployment / request catalog item) is deliberately out of scope — this is a read
core for inventorying a migration source.

Op ids are namespaced ``vcfa.vra8.*`` (distinct from the modern sibling's
``vcfa.provider.*`` / ``vcfa.tenant.*``): the two impls of ``product="vcfa"``
publish different read surfaces (vRA 8's ``/deployment`` + ``/blueprint`` +
``/catalog`` services vs VCFA 9's consolidated ``/iaas`` + ``/cloudapi`` surface),
and the resolver picks the impl per target, so an agent only ever sees the set that
matches the target's version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.vra8._llm_instructions import VRA8_READ_LLM_INSTRUCTIONS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VRA8_TYPED_OPS",
    "VRA8_TYPED_WHEN_TO_USE_BY_GROUP",
    "Vra8TypedOp",
    "register_vra8_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys — the two curated groups the vRA 8 read core spans. Distinct
# from the modern ``vcfa-rest`` group keys so the (product, impl_id, group_key)
# rows never collide across the two impls of product ``vcfa``. Ordered by the
# operator's migration drill: what is running (inventory) -> what defines it (content).
_GROUP_INVENTORY = "vcfa-vra8-inventory"
_GROUP_CONTENT = "vcfa-vra8-content"


@dataclass(frozen=True)
class Vra8TypedOp:
    """Metadata for one vRA 8 typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vra8_typed_operations` can splat the dataclass into
    the helper without per-op boilerplate. ``handler_attr`` is the attribute name
    on :class:`Vra8Connector` exposing the async handler; the registrar resolves
    the bound method against the class at registration time. Mirrors
    :class:`~meho_backplane.connectors.vcd.typed_ops.VcloudDirectorTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group. Every entry is a complete
#: sentence the agent reads verbatim through ``list_operation_groups`` to pick a
#: group to search within; ``register_typed_operation`` requires a non-empty string
#: whenever ``group_key`` is set.
VRA8_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Use this group to inventory the running vRealize Automation 8 estate an "
        "operator is migrating off: projects (the tenancy/quota boundary), "
        "deployments (the running workloads, with their resources and request "
        "state), and the appliance's API capabilities. This is the primary 'what is "
        "deployed and where' surface for migration sizing and cutover quiescence "
        "checks."
    ),
    _GROUP_CONTENT: (
        "Use this group to read the vRA 8 design + catalog surface: Cloud Assembly "
        "blueprints (the infrastructure-as-code definitions a migration must "
        "recreate) and Service Broker catalog items (the self-service offerings to "
        "republish). Use when mapping what the target platform must be able to "
        "rebuild and re-offer."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object — the ``about`` capability read takes no argument.
_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

#: The paged-list parameter object: the OData ``$top`` / ``$skip`` pagination params
#: every vRA 8 list surface accepts (identical lowercase casing), both optional. The
#: read forwards only what the caller sets (see ``._paths.odata_list_query``);
#: sorting/filtering is done through the JSONFlux handle, not the vendor query (MEHO
#: postulate 6 — and vRA 8's ``$orderby`` casing differs across services, so it is
#: deliberately not exposed here).
_ODATA_LIST_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "$top": {
            "type": "integer",
            "minimum": 1,
            "description": "Max items to return (page size). Raise for a whole-estate count.",
        },
        "$skip": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of items to skip (pagination offset).",
        },
    },
    "additionalProperties": False,
}

#: The by-id detail parameter object — a required deployment id.
_DEPLOYMENT_GET_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deployment_id": {
            "type": "string",
            "minLength": 1,
            "description": "The vRA 8 deployment id (UUID), from vcfa.vra8.deployment.list.",
        },
    },
    "required": ["deployment_id"],
    "additionalProperties": False,
}

#: Response schema shared by every vRA 8 read: the list surfaces return a
#: ``{content: [], totalElements, ...}`` wrapper and the detail read a single
#: object, so a permissive object schema fits both.
_OBJECT_RESPONSE: dict[str, Any] = {"type": "object", "additionalProperties": True}

_READ_TAGS = ("read-only", "vcfa", "vra8", "vra")


#: The typed ops :class:`Vra8Connector` registers at lifespan startup — the 6-op
#: vRA 8 migration-inventory read core (#3058). Every op is safe, read-only, and
#: ungated; the wire path/type lives in the handler (see ``.typed_reads`` /
#: ``._paths``), so the descriptor rows carry ``method=None`` / ``path=None`` (typed
#: convention).
VRA8_TYPED_OPS: tuple[Vra8TypedOp, ...] = (
    # ---- Inventory ----
    Vra8TypedOp(
        op_id="vcfa.vra8.project.list",
        handler_attr="project_list",
        summary="List the vRA 8 projects (tenancy/quota boundary).",
        description=(
            "Lists projects via GET /iaas/api/projects — vRA 8's tenancy/quota "
            "boundary and the entry point for a migration inventory. Returns the IaaS "
            "paged {content: [Project], totalElements} wrapper; each Project carries "
            "id, name, orgId, administrators/members, zones, constraints. Server-paged "
            "($top/$skip). Dispatches with zero catalog ingest. safety_level=safe, "
            "read-only."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "project"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.project.list"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.deployment.list",
        handler_attr="deployment_list",
        summary="List the vRA 8 deployments (running workloads).",
        description=(
            "Lists deployments via GET /deployment/api/deployments (the Service Broker "
            "surface) — vRA 8's running workloads, the core migration inventory. "
            "Returns the paged {content: [Deployment], totalElements, totalPages} "
            "wrapper; each Deployment carries id, name, projectId, blueprintId, "
            "catalogItemId, status, ownedBy. A large list is reduced to a JSONFlux "
            "handle. Server-paged ($top/$skip). Dispatches with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "deployment"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.deployment.list"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.deployment.get",
        handler_attr="deployment_get",
        summary="Get one vRA 8 deployment's detail (resources + request state).",
        description=(
            "Reads one deployment via GET /deployment/api/deployments/{deployment_id} "
            "— its resources[] and its request state (lastRequest / "
            "inprogressRequests). vRA 8 has no list-all-requests endpoint, so this is "
            "how request/quiescence state is read. Requires deployment_id (from "
            "deployment.list). Dispatches with zero catalog ingest. safety_level=safe, "
            "read-only."
        ),
        parameter_schema=_DEPLOYMENT_GET_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "deployment"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.deployment.get"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.about",
        handler_attr="about",
        summary="Read the vRA 8 IaaS API capabilities (supported versions).",
        description=(
            "Reads GET /iaas/api/about — {latestApiVersion, supportedApis}. Confirms "
            "the source is a vRA 8.x surface and its API generation. latestApiVersion "
            "is an API *date* label, not the product build. Dispatches with zero "
            "catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "about", "capabilities"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.about"],
    ),
    # ---- Content (design + catalog) ----
    Vra8TypedOp(
        op_id="vcfa.vra8.blueprint.list",
        handler_attr="blueprint_list",
        summary="List the vRA 8 Cloud Assembly blueprints (IaC definitions).",
        description=(
            "Lists blueprints via GET /blueprint/api/blueprints — the Cloud Assembly "
            "infrastructure-as-code definitions a migration must recreate. Returns the "
            "paged {content: [Blueprint], totalElements} wrapper; each Blueprint "
            "carries id, name, projectId, status, content (YAML), totalVersions, "
            "contentSourceType (git-backed source, if any). Server-paged ($top/$skip). "
            "Dispatches with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_CONTENT,
        tags=(*_READ_TAGS, "blueprint"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.blueprint.list"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.catalog-item.list",
        handler_attr="catalog_item_list",
        summary="List the vRA 8 Service Broker catalog items (admin view).",
        description=(
            "Lists catalog items via GET /catalog/api/admin/items (the whole-estate "
            "admin view, not caller-project-scoped) — the self-service offerings a "
            "migration must republish. Returns the paged {content: [CatalogItem], "
            "totalElements} wrapper; each CatalogItem carries id, name, type, "
            "sourceId/sourceName, projectIds. Server-paged ($top/$skip). Dispatches "
            "with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_CONTENT,
        tags=(*_READ_TAGS, "catalog", "catalog-item"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.catalog-item.list"],
    ),
)


async def register_vra8_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VRA8_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after every connector subpackage has been imported, so the
    descriptor rows land before the first dispatch — the vRA 8 read core is live on
    a fresh boot. Idempotent across pod restarts (the helper skips the embedding
    recompute on unchanged summary / description / tags). Mirrors
    :func:`register_vcd_typed_operations`.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so each
    registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide singleton
    when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline (ONNX
    # runtime + model), which pure connector/handler unit tests should not pay.
    # Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vra8.connector import Vra8Connector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VRA8_TYPED_OPS:
        handler = getattr(Vra8Connector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"Vra8Connector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = VRA8_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"Vra8Connector typed op {op.op_id!r} declares group_key="
                f"{op.group_key!r} but no curated when_to_use exists for that key. "
                f"Add an entry to VRA8_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=Vra8Connector.product,
            version=Vra8Connector.version,
            impl_id=Vra8Connector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "vra8_typed_operations_registered",
        count=len(VRA8_TYPED_OPS),
        product=Vra8Connector.product,
        version=Vra8Connector.version,
        impl_id=Vra8Connector.impl_id,
    )

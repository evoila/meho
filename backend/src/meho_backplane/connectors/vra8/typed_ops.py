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

The day-2 ops (:data:`VRA8_DAY2_OPS`) are the exception: they are written once for
both Automation generations in
:mod:`meho_backplane.connectors.vcf_automation.day2_ops` and registered here with
the ``vcfa.vra8.`` prefix.

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

from meho_backplane.connectors.vcf_automation.day2_ops import day2_specs
from meho_backplane.connectors.vra8._llm_instructions import VRA8_READ_LLM_INSTRUCTIONS
from meho_backplane.connectors.vra8._response_schemas import (
    ABOUT_RESPONSE,
    BLUEPRINT_PAGE_RESPONSE,
    CATALOG_ITEM_PAGE_RESPONSE,
    DEPLOYMENT_PAGE_RESPONSE,
    DEPLOYMENT_RESPONSE,
    PROJECT_PAGE_RESPONSE,
)
from meho_backplane.connectors.vra8.api_file import DOCUMENTATION_PATHS, PATH_PATTERN
from meho_backplane.connectors.vra8.api_file import OP_ID as API_FILE_OP_ID

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VRA8_DAY2_OPS",
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
_GROUP_API_FILES = "vcfa-vra8-api-files"
_GROUP_DAY2 = "vcfa-vra8-day2"


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


#: Short text per typed-op group. The agent reads it through
#: ``list_operation_groups`` to pick a group to search in;
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set.
VRA8_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Reads of what runs on a vRealize Automation 8 (Aria Automation 8) system "
        "you are moving off: projects (vcfa.vra8.project.list), deployments with "
        "their resources and request state (vcfa.vra8.deployment.list, "
        "vcfa.vra8.deployment.get), and the API versions (vcfa.vra8.about). Use "
        "them to size a migration and to check that nothing runs before the "
        "switch-over. All reads."
    ),
    _GROUP_CONTENT: (
        "Reads of what vRealize Automation 8 offers: Cloud Assembly blueprints, the "
        "templates a migration must rebuild (vcfa.vra8.blueprint.list), and Service "
        "Broker catalog items, the self-service offers to publish again "
        "(vcfa.vra8.catalog-item.list). All reads."
    ),
    _GROUP_API_FILES: (
        "This group holds one read: vcfa.vra8.api_file.get. It reads one API "
        "description page or file that the Aria Automation 8 appliance serves (the "
        "API landing pages, the Swagger file of one service, or the reference of the "
        "Orchestrator inside the appliance), with its size, SHA-256, and the version "
        "and build of the appliance. Use it to get the 8.18 API files, which no "
        "vendor download contains."
    ),
    _GROUP_DAY2: (
        "Day-2 work on the deployments of Aria Automation 8. Reads: the actions a "
        "deployment or machine offers (vcfa.vra8.deployment.actions.list), the machines "
        "(vcfa.vra8.machine.list) and the disks with their machines "
        "(vcfa.vra8.block_device.list). Changes, each after a person approves: run a "
        "built-in deployment action (vcfa.vra8.deployment.action.run) or machine action "
        "such as power, resize, add disk or snapshot (vcfa.vra8.resource.action.run), "
        "change a lease (vcfa.vra8.deployment.lease.change), create a machine "
        "(vcfa.vra8.machine.create) or a disk for a machine (vcfa.vra8.block_device.create), "
        "and delete a deployment (vcfa.vra8.deployment.delete; only a person can run it, "
        "after a preview)."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object — the ``about`` capability read takes no argument.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

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
            "description": "Items to return (page size). Raise it to count the whole system.",
        },
        "$skip": {
            "type": "integer",
            "minimum": 0,
            "description": "Items to skip (the offset for the next page).",
        },
    },
    "additionalProperties": False,
    "examples": [{"$top": 100, "$skip": 0}],
}

#: The by-id detail parameter object — a required deployment id.
_DEPLOYMENT_GET_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deployment_id": {
            "type": "string",
            "minLength": 1,
            "description": "The deployment id (a UUID), from vcfa.vra8.deployment.list.",
        },
    },
    "required": ["deployment_id"],
    "additionalProperties": False,
    "examples": [{"deployment_id": "<deployment-id>"}],
}

#: The one-file read parameter object. The ``pattern`` is the path rule of
#: :mod:`.api_file`, so the dispatcher refuses a bad path before the handler runs;
#: the handler checks it again. The example is the vendor's documented API landing
#: page, a fixed public path: the rule leaves no room for a placeholder character.
_API_FILE_GET_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 2,
            "maxLength": 512,
            "pattern": PATH_PATTERN,
            "description": (
                "Path of one documentation page or API description file on the target "
                "appliance: /automation/api-docs or /automation-ui/api-docs (the API "
                "landing pages), /<service>/api/swagger and every path under it, "
                "/vco/api/docs and every path under it, or a Swagger or OpenAPI file at "
                "its usual place (a part named api-docs, swagger-resources or "
                "swagger-config, or a file named swagger.json, swagger.yaml, "
                "openapi.json or openapi.yaml). Only letters, digits and . _ ~ / -, no "
                "'..', and a query of only 'group' and 'apiVersion'."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
    "examples": [{"path": DOCUMENTATION_PATHS[0]}],
}

_TEXT_OR_NULL = ["string", "null"]

#: The one-file read response: the scalar file fields, the text in parts, and the
#: version fields. The text parts make it set-shaped, so a large answer goes behind
#: a result handle.
_API_FILE_GET_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The path that was read."},
        "status": {"type": "integer", "description": "HTTP status of the GET."},
        "content_type": {
            "type": _TEXT_OR_NULL,
            "description": "Content-Type header of the answer.",
        },
        "redirect_path": {
            "type": _TEXT_OR_NULL,
            "description": (
                "For a 3xx answer: the path the redirect points to, or scheme://host/path "
                "for another host (not followed)."
            ),
        },
        "size": {
            "type": ["integer", "null"],
            "description": "Size of the checked copy in bytes; null when the status is not 2xx.",
        },
        "sha256": {
            "type": _TEXT_OR_NULL,
            "description": ("SHA-256 (hex) of the checked copy; null when the status is not 2xx."),
        },
        "secrets_replaced": {
            "type": "integer",
            "description": (
                "How many values under a secret name (password, token, licence key, ...) "
                "or other secret text were replaced with a [REDACTED:...] placeholder "
                "in the copy."
            ),
        },
        "parts": {"type": "integer", "description": "How many text parts the file has."},
        "text_parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Position of the part."},
                    "text": {"type": "string", "description": "At most 64 KiB of the copy."},
                },
                "required": ["index", "text"],
            },
            "description": (
                "The text of the checked copy, in order. Joined, they are the copy; its "
                "SHA-256 equals sha256."
            ),
        },
        "version": {
            "type": _TEXT_OR_NULL,
            "description": "Product version from the Orchestrator inside the appliance.",
        },
        "build": {
            "type": _TEXT_OR_NULL,
            "description": "Build number from the Orchestrator inside the appliance.",
        },
        "iaas_api_version": {
            "type": _TEXT_OR_NULL,
            "description": "The IaaS API date label (latestApiVersion), not the build.",
        },
        "version_error": {
            "type": _TEXT_OR_NULL,
            "description": "Why version, build or iaas_api_version is empty; null when all worked.",
        },
    },
    "required": ["path", "status", "parts", "text_parts"],
}

_READ_TAGS = ("read-only", "vcfa", "vra8", "vra")


#: The typed ops :class:`Vra8Connector` registers at lifespan startup — the 6-op
#: vRA 8 migration-inventory read core (#3058) plus the API description read
#: ``vcfa.vra8.api_file.get`` (:mod:`.api_file`). Every op is safe, read-only, and
#: ungated; the wire path/type lives in the handler (see ``.typed_reads`` /
#: ``._paths``), so the descriptor rows carry ``method=None`` / ``path=None`` (typed
#: convention).
VRA8_TYPED_OPS: tuple[Vra8TypedOp, ...] = (
    # ---- Inventory ----
    Vra8TypedOp(
        op_id="vcfa.vra8.project.list",
        handler_attr="project_list",
        summary="List the vRA 8 projects (who may deploy where, with which limits).",
        description=(
            "Lists the projects with GET /iaas/api/projects. In vRA 8 a project sets "
            "who may deploy, to which cloud zones and with which limits. It is the "
            "first read of a migration inventory. Paged with $top and $skip."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=PROJECT_PAGE_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "project"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.project.list"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.deployment.list",
        handler_attr="deployment_list",
        summary="List the vRA 8 deployments (the running workloads).",
        description=(
            "Lists the deployments with GET /deployment/api/deployments (Service "
            "Broker): what runs, in which project, from which blueprint or catalog "
            "item, and its state. A long list comes back as a link to the stored "
            "result. Paged with $top and $skip."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=DEPLOYMENT_PAGE_RESPONSE,
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "deployment"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.deployment.list"],
    ),
    Vra8TypedOp(
        op_id="vcfa.vra8.deployment.get",
        handler_attr="deployment_get",
        summary="Read one vRA 8 deployment: its resources and request state.",
        description=(
            "Reads one deployment with GET /deployment/api/deployments/{id}: its "
            "resources, its last request and the requests that run now. vRA 8 has "
            "no endpoint that lists all requests, so this is how to see them. Needs "
            "deployment_id."
        ),
        parameter_schema=_DEPLOYMENT_GET_PARAMS,
        response_schema=DEPLOYMENT_RESPONSE,
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
            "Reads GET /iaas/api/about: the newest and all supported API versions. "
            "It shows that the system is a vRA 8.x and which API it speaks. "
            "latestApiVersion is a date, not the product version."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=ABOUT_RESPONSE,
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
        summary="List the vRA 8 Cloud Assembly blueprints (deployment templates).",
        description=(
            "Lists the blueprints with GET /blueprint/api/blueprints: the Cloud "
            "Assembly templates (YAML) a migration must rebuild, with their project, "
            "state and versions, and the git source if there is one. Paged with "
            "$top and $skip."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=BLUEPRINT_PAGE_RESPONSE,
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
            "Lists the catalog items with GET /catalog/api/admin/items: the "
            "self-service offers of every project, not only yours. A migration must "
            "publish them again. Paged with $top and $skip."
        ),
        parameter_schema=_ODATA_LIST_PARAMS,
        response_schema=CATALOG_ITEM_PAGE_RESPONSE,
        group_key=_GROUP_CONTENT,
        tags=(*_READ_TAGS, "catalog", "catalog-item"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS["vcfa.vra8.catalog-item.list"],
    ),
    # ---- API description files ----
    Vra8TypedOp(
        op_id=API_FILE_OP_ID,
        handler_attr="api_file_get",
        summary="Read one API description page or file that an Aria Automation 8 appliance serves.",
        description=(
            "Sends one GET with the connector's login to a documentation path (a landing "
            "page, a Swagger file or the Orchestrator reference) and returns its status, "
            "content type, size, SHA-256, version and build, and the text in parts with "
            "secret values replaced. Does not follow redirects. Read-only."
        ),
        parameter_schema=_API_FILE_GET_PARAMS,
        response_schema=_API_FILE_GET_RESPONSE,
        group_key=_GROUP_API_FILES,
        tags=(*_READ_TAGS, "api-description", "swagger", "orchestrator"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VRA8_READ_LLM_INSTRUCTIONS[API_FILE_OP_ID],
    ),
)


#: The day-2 ops, written once for Aria Automation 8 and VCF Automation 9
#: (:mod:`meho_backplane.connectors.vcf_automation.day2_ops`), here with the
#: ``vcfa.vra8.`` prefix. Kept apart from the read core above.
VRA8_DAY2_OPS: tuple[Vra8TypedOp, ...] = tuple(
    Vra8TypedOp(
        op_id=f"vcfa.vra8.{spec.name}",
        handler_attr=spec.handler,
        summary=spec.summary,
        description=spec.description,
        parameter_schema=spec.parameter_schema,
        response_schema=spec.response_schema,
        group_key=_GROUP_DAY2,
        tags=(*spec.tags, "vra8", "vra"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=spec.llm_instructions,
    )
    for spec in day2_specs("vcfa.vra8")
)


async def register_vra8_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VRA8_TYPED_OPS` and :data:`VRA8_DAY2_OPS` as descriptor rows.

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

    all_ops = (*VRA8_TYPED_OPS, *VRA8_DAY2_OPS)
    for op in all_ops:
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
        count=len(all_ops),
        product=Vra8Connector.product,
        version=Vra8Connector.version,
        impl_id=Vra8Connector.impl_id,
    )

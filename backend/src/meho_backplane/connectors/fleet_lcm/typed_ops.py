# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`FleetLcmConnector` (#3047).

The modern VCF 9 Fleet LCM Service ``/v1/*`` read core is registered as
typed ops (``source_kind="typed"``) so it dispatches on a fresh boot with
**zero catalog ingest** — the read core is live the moment the connector
loads. This is what restores 9.0 fleet dispatch (initiative
`evoila/meho#3033 <https://github.com/evoila/meho/issues/3033>`_): under
the "modern default now" resolver decision a VCF Fleet 9.x target resolves
to ``fleet-lcm`` by most-specific-version, and until this read core landed
that impl was a skeleton with no dispatchable ops.

The op bodies live in
:mod:`meho_backplane.connectors.fleet_lcm.typed_reads`; the per-op
``llm_instructions`` in
:mod:`meho_backplane.connectors.fleet_lcm._llm_instructions`; thin
bound-method shims on
:class:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector`
expose them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
recovers each callable from its persisted ``module.ClassName.method`` path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.harbor.typed_ops` (#2856) and
:mod:`meho_backplane.connectors.nsx.typed_ops` (#2302) — the Task #2358
convention: typed ops own the curated read core, generic review
(``ReviewService.enable_reads`` over an operator ingest of the full 51-op
``fleet-lcm-openapi.yaml``) owns the wider write / breadth surface as a
follow-up. The wider surface (component / upgrade-plan / task **writes**)
is deliberately out of scope here — this is a read core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.fleet_lcm._llm_instructions import FLEET_LCM_READ_LLM_INSTRUCTIONS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "FLEET_LCM_TYPED_OPS",
    "FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP",
    "FleetLcmTypedOp",
    "register_fleet_lcm_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys — the five curated groups the fleet-lcm read core
# spans, ordered system -> sddc -> components -> tasks -> lifecycle to match
# the operator's typical "is it up, what's deployed, what's happening, what
# can we upgrade to" drill path.
_GROUP_SYSTEM = "fleet-lcm-system"
_GROUP_SDDC = "fleet-lcm-sddc"
_GROUP_COMPONENTS = "fleet-lcm-components"
_GROUP_TASKS = "fleet-lcm-tasks"
_GROUP_LIFECYCLE = "fleet-lcm-lifecycle"


@dataclass(frozen=True)
class FleetLcmTypedOp:
    """Metadata for one Fleet LCM typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_fleet_lcm_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on :class:`FleetLcmConnector` exposing the async
    handler; the registrar resolves the bound method against the class at
    registration time. Mirrors
    :class:`~meho_backplane.connectors.harbor.typed_ops.HarborTypedOp`.
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


#: Curated ``when_to_use`` blurb per typed-op group. Every entry is a
#: complete sentence the agent reads verbatim through ``list_operation_groups``
#: to pick a group to search within; ``register_typed_operation`` requires a
#: non-empty string whenever ``group_key`` is set.
FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_SYSTEM: (
        "Use this group to read the VCF Fleet LCM service itself: its "
        "liveness (health), the fleet-wide system summary (in-flight upgrade "
        "+ available bundles), and its service configuration. The probe "
        "surface the agent calls first to confirm a 9.x fleet target is "
        "reachable before heavier reads."
    ),
    _GROUP_SDDC: (
        "Use this group to list or inspect the SDDC lifecycle managers the "
        "fleet governs. Each SDDC LCM is the per-SDDC lifecycle authority the "
        "fleet coordinates; use to answer 'what SDDCs does this fleet manage' "
        "or to navigate from an SDDC to its deployed components."
    ),
    _GROUP_COMPONENTS: (
        "Use this group to list or inspect the components the fleet has "
        "deployed and their runtime status — the 'what is deployed and is it "
        "running' inventory. Each component (Operations, Automation, …) "
        "carries its type, version, nodes, and the SDDC LCM it belongs to."
    ),
    _GROUP_TASKS: (
        "Use this group to list or inspect the fleet's async operation tasks "
        "— what the fleet is doing or just did (upgrades, refreshes, "
        "component actions). Use to track an in-flight task's status or to "
        "find the failed task behind a component that is not running."
    ),
    _GROUP_LIFECYCLE: (
        "Use this group to read the fleet's lifecycle surface: the planned "
        "upgrades (upgrade plans) and the release versions the fleet can "
        "target. Use when answering 'what upgrades are planned' or 'what can "
        "we upgrade to', or to compute the delta between deployed and target "
        "versions."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object shared by the no-argument reads (health / list ops).
_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

#: Response schema shared by every fleet-lcm read: the by-id reads return a
#: single object and the list reads return an object envelope
#: (``{components: []}`` / ``{sddcLcms: []}`` / ``{elements: [], pageMetadata}``),
#: so a permissive object schema fits both.
_OBJECT_RESPONSE: dict[str, Any] = {"type": "object", "additionalProperties": True}

_SDDC_LCM_ID_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "SDDC LCM UUID, from fleet-lcm.sddc-lcm.list.",
}
_COMPONENT_ID_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Managed component UUID, from fleet-lcm.component.list.",
}
_TASK_ID_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Async task UUID, from fleet-lcm.task.list.",
}
_PLAN_ID_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Upgrade plan UUID, from fleet-lcm.upgrade-plan.list.",
}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build a closed (``additionalProperties=False``) object schema."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


#: The typed ops :class:`FleetLcmConnector` registers at lifespan startup —
#: the 13-op modern read core (#3047). Every op is safe, read-only, and
#: ungated; the wire path lives in the handler (see ``._paths``), so the
#: descriptor rows carry ``method=None`` / ``path=None`` (typed convention).
FLEET_LCM_TYPED_OPS: tuple[FleetLcmTypedOp, ...] = (
    # ---- System / service ----
    FleetLcmTypedOp(
        op_id="fleet-lcm.health",
        handler_attr="health",
        summary="Fleet LCM service liveness probe.",
        description=(
            "Returns the Fleet LCM Service liveness via GET /v1/health ({up: bool}). The "
            "modern 9.x successor to the legacy fleet-rest about/health probe (which 500s "
            "on 9.0). The same reachability surface the connector's probe reads, exposed "
            "as a typed op. Dispatches with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SYSTEM,
        tags=("read-only", "fleet-lcm", "vcf", "health"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.health"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.system.info",
        handler_attr="system_info",
        summary="Fleet-wide system summary: in-flight upgrade + available bundles.",
        description=(
            "Returns the fleet system summary via GET /v1/system: any in-flight system "
            "upgrade (taskId + status) and the software bundles[] available to the fleet. "
            "Dispatches with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SYSTEM,
        tags=("read-only", "fleet-lcm", "vcf", "system"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.system.info"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.config.info",
        handler_attr="config_info",
        summary="Fleet LCM service configuration.",
        description=(
            "Returns the Fleet LCM service configuration via GET /v1/config: "
            "deploymentFlavor, the management vspCluster it is bound to, config id / "
            "version, and overall status (HEALTHY / OFFLINE). Dispatches with zero catalog "
            "ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SYSTEM,
        tags=("read-only", "fleet-lcm", "vcf", "config"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.config.info"],
    ),
    # ---- SDDC LCM inventory ----
    FleetLcmTypedOp(
        op_id="fleet-lcm.sddc-lcm.list",
        handler_attr="sddc_lcm_list",
        summary="List the SDDC lifecycle managers the fleet governs.",
        description=(
            "Lists SDDC lifecycle managers via GET /v1/sddc-lcms — the 'what SDDCs does "
            "this fleet manage' entry point. Returns a paged {pageMetadata, sddcLcms: []} "
            "envelope. Dispatches with zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SDDC,
        tags=("read-only", "fleet-lcm", "vcf", "sddc"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.sddc-lcm.list"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.sddc-lcm.info",
        handler_attr="sddc_lcm_info",
        summary="Full detail for one SDDC lifecycle manager.",
        description=(
            "Reads one SDDC lifecycle manager by id via GET /v1/sddc-lcms/{sddcLcmId}: "
            "fqdn, sddcGroupFqdn, isPrimary, and vspClusters[]. Requires sddcLcmId from "
            "fleet-lcm.sddc-lcm.list. Dispatches with zero catalog ingest. safety_level=safe, "
            "read-only."
        ),
        parameter_schema=_schema({"sddcLcmId": _SDDC_LCM_ID_PROP}, ("sddcLcmId",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_SDDC,
        tags=("read-only", "fleet-lcm", "vcf", "sddc"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.sddc-lcm.info"],
    ),
    # ---- Managed components ----
    FleetLcmTypedOp(
        op_id="fleet-lcm.component.list",
        handler_attr="component_list",
        summary="List the components the fleet has deployed.",
        description=(
            "Lists managed components via GET /v1/components — the primary 'what is "
            "deployed' inventory. Returns {components: []}; each carries componentType, "
            "fqdn, version, sddcLcmId, deploymentType, nodes[]. A large inventory is "
            "reduced to a JSONFlux handle. Dispatches with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_COMPONENTS,
        tags=("read-only", "fleet-lcm", "vcf", "component"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.component.list"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.component.info",
        handler_attr="component_info",
        summary="Full detail for one managed component.",
        description=(
            "Reads one managed component by id via GET /v1/components/{componentId}: "
            "componentType, fqdn, version, sddcLcmId, deploymentType, nodes[], vspCluster. "
            "Requires componentId from fleet-lcm.component.list. Dispatches with zero "
            "catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"componentId": _COMPONENT_ID_PROP}, ("componentId",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_COMPONENTS,
        tags=("read-only", "fleet-lcm", "vcf", "component"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.component.info"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.component.status",
        handler_attr="component_status",
        summary="Runtime status of one managed component.",
        description=(
            "Reads the runtime status of one managed component via "
            "GET /v1/components/{componentId}/status: {id, status} where status is "
            "Unknown / NotRunning / Running. Requires componentId from "
            "fleet-lcm.component.list. Dispatches with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"componentId": _COMPONENT_ID_PROP}, ("componentId",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_COMPONENTS,
        tags=("read-only", "fleet-lcm", "vcf", "component", "status"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.component.status"],
    ),
    # ---- Async tasks ----
    FleetLcmTypedOp(
        op_id="fleet-lcm.task.list",
        handler_attr="task_list",
        summary="List the fleet's async operation tasks.",
        description=(
            "Lists async operation tasks via GET /v1/tasks — 'what is the fleet doing / "
            "just did'. Returns a paged {elements: [], pageMetadata} envelope; each task "
            "carries resourceId, type, timing, and a localizedMessage. A large list is "
            "reduced to a JSONFlux handle. Dispatches with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_TASKS,
        tags=("read-only", "fleet-lcm", "vcf", "task"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.task.list"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.task.info",
        handler_attr="task_info",
        summary="Full detail for one async task.",
        description=(
            "Reads one async task by id via GET /v1/tasks/{taskId}: status, timing, "
            "createdBy / updatedBy, cancellable / retriable, parentTaskId, and a "
            "localizedMessage. Requires taskId from fleet-lcm.task.list. Dispatches with "
            "zero catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"taskId": _TASK_ID_PROP}, ("taskId",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_TASKS,
        tags=("read-only", "fleet-lcm", "vcf", "task"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.task.info"],
    ),
    # ---- Lifecycle — upgrade plans + release versions ----
    FleetLcmTypedOp(
        op_id="fleet-lcm.upgrade-plan.list",
        handler_attr="upgrade_plan_list",
        summary="List the fleet's upgrade plans.",
        description=(
            "Lists upgrade plans via GET /v1/upgrade-plans — 'what upgrades are planned'. "
            "Returns a paged {elements: [], pageMetadata} envelope; each plan pins a "
            "desiredSoftware set against a componentsFilter. Dispatches with zero catalog "
            "ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_LIFECYCLE,
        tags=("read-only", "fleet-lcm", "vcf", "upgrade"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.upgrade-plan.list"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.upgrade-plan.info",
        handler_attr="upgrade_plan_info",
        summary="Full detail for one upgrade plan.",
        description=(
            "Reads one upgrade plan by id via GET /v1/upgrade-plans/{planId}: its spec "
            "(componentsFilter + desiredSoftware) and timing. Requires planId from "
            "fleet-lcm.upgrade-plan.list. Dispatches with zero catalog ingest. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_schema({"planId": _PLAN_ID_PROP}, ("planId",)),
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_LIFECYCLE,
        tags=("read-only", "fleet-lcm", "vcf", "upgrade"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.upgrade-plan.info"],
    ),
    FleetLcmTypedOp(
        op_id="fleet-lcm.release-version.list",
        handler_attr="release_version_list",
        summary="List the release versions the fleet can target.",
        description=(
            "Lists targetable release versions via GET /v1/release-versions — 'what can we "
            "upgrade to'. Returns a paged {elements: [], pageMetadata} envelope; each entry "
            "carries a version and per-component available versions. Dispatches with zero "
            "catalog ingest. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_OBJECT_RESPONSE,
        group_key=_GROUP_LIFECYCLE,
        tags=("read-only", "fleet-lcm", "vcf", "release"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS["fleet-lcm.release-version.list"],
    ),
)


async def register_fleet_lcm_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`FLEET_LCM_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after every connector subpackage has been imported, so the
    descriptor rows land before the first dispatch — the 9.x fleet read core
    is live on a fresh boot. Idempotent across pod restarts (the helper skips
    the embedding recompute on unchanged summary / description / tags).
    Mirrors :func:`register_harbor_typed_operations`.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should
    # not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.fleet_lcm.connector import FleetLcmConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in FLEET_LCM_TYPED_OPS:
        handler = getattr(FleetLcmConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"FleetLcmConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"FleetLcmConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                f"but no curated when_to_use exists for that key. Add an entry to "
                f"FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=FleetLcmConnector.product,
            version=FleetLcmConnector.version,
            impl_id=FleetLcmConnector.impl_id,
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
        "fleet_lcm_typed_operations_registered",
        count=len(FLEET_LCM_TYPED_OPS),
        product=FleetLcmConnector.product,
        version=FleetLcmConnector.version,
        impl_id=FleetLcmConnector.impl_id,
    )

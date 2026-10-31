# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`FleetLcmConnector` (#3047).

The modern VCF 9 Fleet LCM Service ``/v1/*`` read core is registered as
typed ops (``source_kind="typed"``) so it dispatches on a fresh boot with
**zero catalog ingest** — the read core is live the moment the connector
loads (initiative
`evoila/meho#3033 <https://github.com/evoila/meho/issues/3033>`_). A VCF
Fleet 9.1 or newer target resolves to ``fleet-lcm`` (``>=9.1,<10.0``), and
until this read core landed that impl was a skeleton with no dispatchable
ops.

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
follow-up. The six lifecycle changes (retry and cancel a task; create,
pre-check and apply an upgrade plan; sync the depot) live in
:mod:`meho_backplane.connectors.fleet_lcm.lifecycle_ops` and are appended
to :data:`FLEET_LCM_TYPED_OPS`, so this registrar registers them too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.connectors.fleet_lcm._llm_instructions import FLEET_LCM_READ_LLM_INSTRUCTIONS
from meho_backplane.connectors.fleet_lcm._response_schemas import FLEET_LCM_RESPONSE_SCHEMAS
from meho_backplane.connectors.fleet_lcm.lifecycle_ops import (
    FLEET_LCM_LIFECYCLE_OPS,
    GROUP_LIFECYCLE,
    GROUP_TASKS,
)
from meho_backplane.connectors.fleet_lcm.op_types import FleetLcmTypedOp

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
_GROUP_TASKS = GROUP_TASKS
_GROUP_LIFECYCLE = GROUP_LIFECYCLE


#: Short text per typed-op group. The agent reads it through
#: ``list_operation_groups`` to pick a group, so it says which reads each group
#: holds. ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set.
FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_SYSTEM: (
        "Reads of the VCF 9 Fleet LCM service itself: whether it is up, the fleet "
        "version with the upgrade that runs now and the bundles it can use, and the "
        "service configuration. Call health first. All reads."
    ),
    _GROUP_SDDC: (
        "Reads of the SDDC lifecycle managers the fleet manages: list them, or read "
        "one by id. Use it to see which SDDCs the fleet covers. All reads."
    ),
    _GROUP_COMPONENTS: (
        "Reads of the components the fleet has deployed (VCF Operations, VCF "
        "Automation and more): list them, read one by id, or check whether one "
        "runs. All reads."
    ),
    _GROUP_TASKS: (
        "The fleet's tasks (upgrades, prechecks, depot syncs, component actions). "
        "Two reads: list the tasks, or read one by id with its state, stages and "
        "errors (the status read for every fleet job). Two changes: retry a failed "
        "task, or cancel a running one."
    ),
    _GROUP_LIFECYCLE: (
        "Upgrade planning and the depot. Three reads: the upgrade plans, one plan "
        "by id with its components and runs, and the release versions the fleet can "
        "move to. Four changes: create an upgrade plan (it is checked first), run "
        "its pre-check, apply it (start the upgrades), and sync the depot metadata."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object shared by the no-argument reads (health / list ops).
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

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
    "description": "Task UUID, from fleet-lcm.task.list or fleet-lcm.system.info.",
}
_PLAN_ID_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Upgrade plan UUID, from fleet-lcm.upgrade-plan.list.",
}


def _schema(name: str, prop: dict[str, Any], placeholder: str) -> dict[str, Any]:
    """Build the closed one-id parameter schema of a by-id read, with one example."""
    return {
        "type": "object",
        "properties": {name: prop},
        "required": [name],
        "additionalProperties": False,
        "examples": [{name: placeholder}],
    }


def _op(
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    group_key: str,
    tags: tuple[str, ...],
    parameter_schema: dict[str, Any] = _NO_PARAMS,
) -> FleetLcmTypedOp:
    """Build one safe, read-only, ungated fleet-lcm read.

    The response schema and ``llm_instructions`` come from the per-op tables,
    keyed by ``op_id``.
    """
    return FleetLcmTypedOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        parameter_schema=parameter_schema,
        response_schema=FLEET_LCM_RESPONSE_SCHEMAS[op_id],
        group_key=group_key,
        tags=("read-only", "fleet-lcm", "vcf", *tags),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=FLEET_LCM_READ_LLM_INSTRUCTIONS[op_id],
    )


#: The typed ops :class:`FleetLcmConnector` registers at lifespan startup:
#: the 13 safe reads, then the six changes of :mod:`.lifecycle_ops` (each
#: waits for approval). The wire path lives in the handler (see ``._paths``),
#: so the descriptor rows carry ``method=None`` / ``path=None`` (typed
#: convention).
FLEET_LCM_TYPED_OPS: tuple[FleetLcmTypedOp, ...] = (
    # ---- System / service ----
    _op(
        "fleet-lcm.health",
        "health",
        "Check whether the VCF 9 Fleet LCM service is up.",
        "Reads GET /v1/health: one 'up' value, true or false. The VCF 9 way to check "
        "the fleet service; the older fleet.about read fails on 9.0. Read-only.",
        _GROUP_SYSTEM,
        ("health",),
    ),
    _op(
        "fleet-lcm.system.info",
        "system_info",
        "Read the fleet version, the upgrade that runs now and the available bundles.",
        "Reads GET /v1/system: the current and target fleet version, the versions it "
        "can move to, a system upgrade that runs now (task id and state) and the "
        "software bundles per fleet depot. Read-only.",
        _GROUP_SYSTEM,
        ("system",),
    ),
    _op(
        "fleet-lcm.config.info",
        "config_info",
        "Read the Fleet LCM service configuration.",
        "Reads GET /v1/config: the deployment flavor, the management cluster the "
        "service runs on, the service version and its state (HEALTHY or OFFLINE). "
        "Read-only.",
        _GROUP_SYSTEM,
        ("config",),
    ),
    # ---- SDDC LCM inventory ----
    _op(
        "fleet-lcm.sddc-lcm.list",
        "sddc_lcm_list",
        "List the SDDC lifecycle managers the fleet manages.",
        "Lists the SDDC lifecycle managers via GET /v1/sddc-lcms, with each one's "
        "FQDN, SDDC group, vCenter, clusters and state. Answers 'which SDDCs does "
        "this fleet manage'. Read-only.",
        _GROUP_SDDC,
        ("sddc",),
    ),
    _op(
        "fleet-lcm.sddc-lcm.info",
        "sddc_lcm_info",
        "Read one SDDC lifecycle manager by id.",
        "Reads one SDDC lifecycle manager via GET /v1/sddc-lcms/{sddcLcmId}: FQDN, "
        "SDDC group, vCenter, clusters, fleet depot and state. Needs an id from "
        "fleet-lcm.sddc-lcm.list. Read-only.",
        _GROUP_SDDC,
        ("sddc",),
        _schema("sddcLcmId", _SDDC_LCM_ID_PROP, "<sddc-lcm-id>"),
    ),
    # ---- Managed components ----
    _op(
        "fleet-lcm.component.list",
        "component_list",
        "List the components the fleet has deployed.",
        "Lists the managed components via GET /v1/components: type, version, FQDN, "
        "nodes, cluster and SDDC LCM of each. The main 'what is deployed' read. A "
        "large list comes back as a result handle. Read-only.",
        _GROUP_COMPONENTS,
        ("component",),
    ),
    _op(
        "fleet-lcm.component.info",
        "component_info",
        "Read one managed component by id.",
        "Reads one managed component via GET /v1/components/{componentId}: type, "
        "version, FQDN, nodes, cluster and SDDC LCM. Needs an id from "
        "fleet-lcm.component.list. Read-only.",
        _GROUP_COMPONENTS,
        ("component",),
        _schema("componentId", _COMPONENT_ID_PROP, "<component-id>"),
    ),
    _op(
        "fleet-lcm.component.status",
        "component_status",
        "Check whether one managed component runs.",
        "Reads GET /v1/components/{componentId}/status: the component id and its "
        "state, Unknown, NotRunning or Running. Needs an id from "
        "fleet-lcm.component.list. Read-only.",
        _GROUP_COMPONENTS,
        ("component", "status"),
        _schema("componentId", _COMPONENT_ID_PROP, "<component-id>"),
    ),
    # ---- Async tasks ----
    _op(
        "fleet-lcm.task.list",
        "task_list",
        "List the fleet's tasks, running or recently ended.",
        "Lists the fleet's tasks via GET /v1/tasks: name, type, state, times, the "
        "object each task works on and a message. A large list comes back as a "
        "result handle. Read-only.",
        _GROUP_TASKS,
        ("task",),
    ),
    _op(
        "fleet-lcm.task.info",
        "task_info",
        "Read one fleet task by id: state, progress, stages and error text.",
        "Reads GET /v1/tasks/{taskId}: the vendor task plus a plain state, the stage "
        "progress and the error texts. Says not_reachable, not failed, when the "
        "service does not answer. The status read of every fleet job. Read-only.",
        _GROUP_TASKS,
        ("task",),
        _schema("taskId", _TASK_ID_PROP, "<task-id>"),
    ),
    # ---- Lifecycle — upgrade plans + release versions ----
    _op(
        "fleet-lcm.upgrade-plan.list",
        "upgrade_plan_list",
        "List the fleet's upgrade plans.",
        "Lists the upgrade plans via GET /v1/upgrade-plans: each plan's state, the "
        "target software and the components it covers. Answers 'which upgrades are "
        "planned'. Read-only.",
        _GROUP_LIFECYCLE,
        ("upgrade",),
    ),
    _op(
        "fleet-lcm.upgrade-plan.info",
        "upgrade_plan_info",
        "Read one upgrade plan by id, with its runs and components.",
        "Reads one upgrade plan via GET /v1/upgrade-plans/{planId}: state, target "
        "software, its runs (upgrades and prechecks) and each component's current "
        "and target version. Needs an id from fleet-lcm.upgrade-plan.list. Read-only.",
        _GROUP_LIFECYCLE,
        ("upgrade",),
        _schema("planId", _PLAN_ID_PROP, "<upgrade-plan-id>"),
    ),
    _op(
        "fleet-lcm.release-version.list",
        "release_version_list",
        "List the release versions the fleet can upgrade to.",
        "Lists the release versions via GET /v1/release-versions, each with the "
        "versions per component type. Answers 'what can we upgrade to'. Read-only.",
        _GROUP_LIFECYCLE,
        ("release",),
    ),
    *FLEET_LCM_LIFECYCLE_OPS,
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

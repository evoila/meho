# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas for the ``fleet-lcm`` typed reads.

Each schema names the main fields of the vendor answer, so the AI knows what a
read returns before it calls it. The field names come from the VCF Fleet LCM
Service OpenAPI file (``fleet-lcm-openapi.yaml``, ``info.version`` 9.1.0.0, and
its 9.1.1 successor). The schemas are informational: the reducer does not read
them, and every object stays open (``additionalProperties: true``) so a field
a newer build adds is not hidden.

Split out of :mod:`~meho_backplane.connectors.fleet_lcm.typed_ops` to keep that
module under the file-size budget.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FLEET_LCM_RESPONSE_SCHEMAS"]


def _field(type_: str, description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


_PAGE_METADATA: dict[str, Any] = _object(
    "Paging data for the list.",
    {
        "pageNumber": _field("integer", "The current page number."),
        "pageSize": _field("integer", "The number of elements in this page."),
        "totalElements": _field("integer", "The total number of elements."),
        "totalPages": _field("integer", "The total number of pages."),
    },
)

_VSP_CLUSTER: dict[str, Any] = {
    "id": _field("string", "The cluster id."),
    "fqdn": _field("string", "The cluster FQDN."),
    "type": _field("string", "MANAGEMENT or CONSUMPTION."),
}

_VCENTER: dict[str, Any] = _object(
    "The vCenter this belongs to.",
    {
        "id": _field("string", "The vCenter id."),
        "fqdn": _field("string", "The vCenter FQDN."),
    },
)

_MESSAGE: dict[str, Any] = _object(
    "A text from the service.",
    {
        "id": _field("string", "The message id."),
        "defaultMessage": _field("string", "The text in English."),
        "localizedMessage": _field("string", "The text in the user's language."),
    },
)

_SDDC_LCM_FIELDS: dict[str, Any] = {
    "id": _field("string", "The SDDC LCM id (a UUID)."),
    "fqdn": _field("string", "The SDDC LCM FQDN."),
    "isPrimary": _field("boolean", "Whether this is the primary SDDC LCM of the fleet."),
    "sddcGroupId": _field("string", "The id of the SDDC group."),
    "sddcGroupName": _field("string", "The name of the SDDC group."),
    "sddcGroupFqdn": _field("string", "The FQDN of the SDDC group."),
    "vcenter": _VCENTER,
    "vspClusters": _list_of("The clusters the SDDC LCM runs on.", _VSP_CLUSTER),
    "fleetDepot": _object(
        "The fleet depot the SDDC LCM uses.",
        {"fqdn": _field("string", "The depot FQDN.")},
    ),
    "status": _field("string", "HEALTHY or OFFLINE."),
}

_COMPONENT_FIELDS: dict[str, Any] = {
    "id": _field("string", "The component id (a UUID)."),
    "componentType": _field("string", "The component type, for example OPS or VCFA."),
    "componentTypeDescription": _field("string", "The component type in words."),
    "deploymentType": _field("string", "OVA or VSP."),
    "version": _field("string", "The deployed version."),
    "size": _field("string", "The deployment size."),
    "fqdn": _field("string", "The component FQDN."),
    "scope": _field("string", "FLEET or INSTANCE."),
    "sddcLcmId": _field("string", "The SDDC LCM the component belongs to."),
    "nodes": _list_of(
        "The nodes of the component.",
        {
            "id": _field("string", "The node id."),
            "nodeType": _field("string", "The node type."),
            "name": _field("string", "The node name."),
            "fqdn": _field("string", "The node FQDN."),
            "ipAddress": _field("string", "The node IP."),
            "version": _field("string", "The node version."),
            "status": _field("string", "The node state."),
            "size": _field("string", "The node size."),
        },
    ),
    "vcenter": _VCENTER,
    "vspCluster": _object("The cluster the component runs on.", _VSP_CLUSTER),
}

_TASK_SUMMARY_FIELDS: dict[str, Any] = {
    "id": _field("string", "The task id."),
    "name": _field("string", "The task name."),
    "description": _MESSAGE,
    "status": _field("string", "PENDING, SCHEDULED, RUNNING, SUCCEEDED, FAILED or CANCELED."),
    "type": _field("string", "The task type."),
    "resourceId": _field("string", "The id of the object the task works on."),
    "resourceType": _field("string", "The type of the object the task works on."),
    "createdBy": _field("string", "Who started the task."),
    "createTime": _field("string", "When the task was created."),
    "startTime": _field("string", "When the task started."),
    "updateTime": _field("string", "When the task last changed."),
    "endTime": _field("string", "When the task ended."),
    "parentTaskId": _field("string", "The parent task, for a sub-task."),
    "retriable": _field("boolean", "Whether the task can be run again."),
    "cancellable": _field("boolean", "Whether the task can be cancelled."),
}

_DESIRED_SOFTWARE: dict[str, Any] = _object(
    "The software the plan moves to.",
    {
        "version": _field("string", "The target release version."),
        "components": _list_of(
            "The target version per component type.",
            {
                "type": _field("string", "The component type, for example OPS."),
                "publicName": _field("string", "The display name of the component."),
                "version": _field("string", "The target version."),
            },
        ),
    },
)

_UPGRADE_PLAN_FIELDS: dict[str, Any] = {
    "id": _field("string", "The upgrade plan id (a UUID)."),
    "status": _field("string", "DRAFT, SYNCING, READY or COMPLETED."),
    "spec": _object(
        "What the plan upgrades and to what.",
        {
            "desiredSoftware": _DESIRED_SOFTWARE,
            "componentsFilter": _list_of(
                "The components the plan covers.",
                {"id": _field("string", "The component id.")},
            ),
        },
    ),
    "createTime": _field("string", "When the plan was created."),
    "createdBy": _field("string", "Who created the plan."),
    "updateTime": _field("string", "When the plan last changed."),
}

#: Response schema per op id, consumed by
#: :mod:`meho_backplane.connectors.fleet_lcm.typed_ops`.
FLEET_LCM_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "fleet-lcm.health": _object(
        "The service liveness.",
        {"up": _field("boolean", "Whether the Fleet LCM service is up.")},
    ),
    "fleet-lcm.system.info": _object(
        "The fleet system summary.",
        {
            "currentVersion": _field("string", "The fleet version now."),
            "targetVersion": _field("string", "The version a running upgrade moves to."),
            "availableTargetVersions": {
                "type": "array",
                "description": "The versions the fleet can upgrade to.",
                "items": {"type": "string"},
            },
            "upgrade": _object(
                "The system upgrade that runs now, if any.",
                {
                    "taskId": _field("string", "The upgrade task id."),
                    "status": _field("string", "The upgrade task state."),
                },
            ),
            "bundles": _list_of(
                "The software bundles the fleet can use, per fleet depot.",
                {
                    "fdsFqdn": _field("string", "The FQDN of the fleet depot."),
                    "components": _list_of(
                        "The bundles per component.",
                        {
                            "componentType": _field("string", "The component type."),
                            "publicName": _field("string", "The display name."),
                            "bundleId": _field("string", "The bundle id."),
                            "bundleType": _field("string", "The bundle type."),
                            "version": _field("string", "The bundle version."),
                            "status": _field(
                                "string", "REQUIRED, NOT_REQUIRED or ALREADY_UPGRADED."
                            ),
                        },
                    ),
                },
            ),
        },
    ),
    "fleet-lcm.config.info": _object(
        "The Fleet LCM service configuration.",
        {
            "id": _field("string", "The config id (a UUID)."),
            "version": _field("string", "The service version."),
            "status": _field("string", "HEALTHY or OFFLINE."),
            "deploymentFlavor": _field("string", "The deployment flavor, for example VCF."),
            "vspCluster": _object("The cluster the service runs on.", _VSP_CLUSTER),
        },
    ),
    "fleet-lcm.sddc-lcm.list": _object(
        "A page of SDDC lifecycle managers.",
        {
            "sddcLcms": _list_of("The SDDC lifecycle managers.", _SDDC_LCM_FIELDS),
            "pageMetadata": _PAGE_METADATA,
        },
    ),
    "fleet-lcm.sddc-lcm.info": _object("One SDDC lifecycle manager.", _SDDC_LCM_FIELDS),
    "fleet-lcm.component.list": _object(
        "A page of managed components.",
        {
            "components": _list_of("The managed components.", _COMPONENT_FIELDS),
            "pageMetadata": _PAGE_METADATA,
        },
    ),
    "fleet-lcm.component.info": _object("One managed component.", _COMPONENT_FIELDS),
    "fleet-lcm.component.status": _object(
        "The runtime status of one component.",
        {
            "id": _field("string", "The component id."),
            "status": _field("string", "Unknown, NotRunning or Running."),
        },
    ),
    "fleet-lcm.task.list": _object(
        "A page of tasks.",
        {
            "elements": _list_of("The tasks.", _TASK_SUMMARY_FIELDS),
            "pageMetadata": _PAGE_METADATA,
        },
    ),
    "fleet-lcm.task.info": _object(
        "One task with its stages and sub-tasks.",
        {
            **_TASK_SUMMARY_FIELDS,
            "updatedBy": _field("string", "Who last changed the task."),
            "taskSummary": _object(
                "Counts of the task's parts.",
                {
                    "totalSubTasks": _field("integer", "The number of sub-tasks."),
                    "totalSteps": _field("integer", "The number of steps."),
                },
            ),
            "stages": _list_of(
                "The stages of the task.",
                {
                    "id": _field("string", "The stage id."),
                    "name": _field("string", "The stage name."),
                    "status": _field("string", "The stage state."),
                },
            ),
            "subTasks": {
                "type": "array",
                "description": "The sub-tasks, each with the same shape as a task.",
                "items": {"type": "object"},
            },
            "messages": _list_of(
                "The messages of the task.",
                {
                    "timestamp": _field("string", "When the message was written."),
                    "level": _field("string", "INFO, WARN or ERROR."),
                    "message": _MESSAGE,
                },
            ),
            "result": _field("object", "The task result, for example a precheck result."),
            "task_id": _field("string", "The task id (also when the service did not answer)."),
            "state": _field(
                "string",
                "MEHO's plain state: queued, running, succeeded, failed, canceled, unknown, "
                "or not_reachable (the service did not answer; not a failure).",
            ),
            "progress": _object(
                "Stage progress; null when the vendor lists no stages.",
                {
                    "stages_total": _field("integer", "The number of stages."),
                    "stages_done": _field("integer", "Stages that succeeded or were skipped."),
                    "current_stage": _field("string", "The stage that runs now, if any."),
                },
            ),
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The ERROR texts of the task, its stages and its sub-tasks.",
            },
            "reachable": _field("boolean", "False when the service did not answer."),
            "unreachable_reason": _field("string", "Why the read got no answer."),
            "checked_at": _field("string", "When MEHO read the task (ISO 8601, UTC)."),
        },
    ),
    "fleet-lcm.upgrade-plan.list": _object(
        "A page of upgrade plans.",
        {
            "elements": _list_of("The upgrade plans.", _UPGRADE_PLAN_FIELDS),
            "pageMetadata": _PAGE_METADATA,
        },
    ),
    "fleet-lcm.upgrade-plan.info": _object(
        "One upgrade plan with its runs and components.",
        {
            **_UPGRADE_PLAN_FIELDS,
            "executions": _list_of(
                "The runs of the plan: upgrades and prechecks.",
                {
                    "taskId": _field("string", "The task id of the run."),
                    "type": _field("string", "UPGRADE or PRECHECK."),
                    "status": _field("string", "The run state."),
                    "createTime": _field("string", "When the run started."),
                },
            ),
            "components": _object(
                "A page of the components in the plan.",
                {
                    "elements": _list_of(
                        "The components in the plan.",
                        {
                            "id": _field("string", "The component id."),
                            "type": _field("string", "The component type."),
                            "fqdn": _field("string", "The component FQDN."),
                            "version": _field("string", "The version now."),
                            "targetVersion": _field("string", "The version after upgrade."),
                            "status": _field(
                                "string",
                                "ELIGIBLE, PENDING, INELIGIBLE, ON_TARGET or HIGHER_THAN_TARGET.",
                            ),
                        },
                    ),
                    "pageMetadata": _PAGE_METADATA,
                },
            ),
        },
    ),
    "fleet-lcm.release-version.list": _object(
        "A page of release versions.",
        {
            "elements": _list_of(
                "The release versions.",
                {
                    "version": _field("string", "The release version."),
                    "components": _list_of(
                        "The versions per component type.",
                        {
                            "type": _field("string", "The component type, for example OPS."),
                            "publicName": _field("string", "The display name."),
                            "versions": {
                                "type": "array",
                                "description": "The versions, newest first.",
                                "items": {"type": "string"},
                            },
                        },
                    ),
                },
            ),
            "pageMetadata": _PAGE_METADATA,
        },
    ),
}

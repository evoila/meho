# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op ``llm_instructions`` blobs for the ``fleet-lcm`` typed read core.

Split out of :mod:`~meho_backplane.connectors.fleet_lcm.typed_reads` (which
holds the op bodies) purely to keep each module under the file-size budget;
harbor (#2856) co-locates the equivalent ``HARBOR_READ_LLM_INSTRUCTIONS``
with its read impls, but the 13-op fleet-lcm core is large enough to earn a
dedicated module. Consumed by
:mod:`meho_backplane.connectors.fleet_lcm.typed_ops`, which keys each op's
metadata row to :data:`FLEET_LCM_READ_LLM_INSTRUCTIONS`.

The canonical three-key shape (``when_to_call`` / ``output_shape`` /
``next_step``) is the same convention the harbor / nsx / hetzner read cores
use, so an agent crossing connector boundaries sees a stable structure. The
prose references the dot-form op ids the reads register under and the
sibling ops an agent drills to next.
"""

from __future__ import annotations

__all__ = ["FLEET_LCM_READ_LLM_INSTRUCTIONS"]


_HEALTH_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to check whether the VCF Fleet LCM Service is up before any "
        "heavier fleet read, or when diagnosing a reachability issue. This "
        "is the modern 9.x successor to the legacy fleet-rest about/health "
        "probe (which returns HTTP 500 on 9.0 appliances) — on a 9.x fleet "
        "target this endpoint answers cleanly."
    ),
    "output_shape": "Object with a single 'up' boolean.",
    "next_step": (
        "If up=true, proceed to fleet-lcm.system.info for the fleet summary "
        "or fleet-lcm.component.list for the deployed inventory. If up=false "
        "or the call errors, the service is unreachable — surface that to the "
        "operator rather than retrying other fleet reads."
    ),
}

_SYSTEM_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read the fleet-wide system summary: whether a system-level "
        "upgrade is in flight and which software bundles are available to "
        "the fleet. Use when answering 'is the fleet mid-upgrade' or 'what "
        "bundles can the fleet deploy'."
    ),
    "output_shape": (
        "Object with 'upgrade' ({taskId, status e.g. PENDING}) and "
        "'bundles' [] — each bundle carries components[] with componentType, "
        "publicName, bundleId, bundleType, version, and status (e.g. "
        "REQUIRED)."
    ),
    "next_step": (
        "If upgrade.taskId is set, pass it to fleet-lcm.task.info to track "
        "the in-flight system upgrade. For per-component upgrade planning, "
        "see fleet-lcm.upgrade-plan.list."
    ),
}

_CONFIG_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read how the Fleet LCM service itself is configured: its "
        "deployment flavor, the management cluster it is bound to, and its "
        "overall health status. Use when confirming which SDDC the fleet "
        "service runs against or diagnosing a service-config issue."
    ),
    "output_shape": (
        "Object with deploymentFlavor (e.g. 'VCF'), vspCluster (fqdn, id, "
        "type e.g. MANAGEMENT), id (uuid), version, and status (HEALTHY / "
        "OFFLINE)."
    ),
    "next_step": (
        "Cross-reference vspCluster.fqdn with fleet-lcm.component.list to "
        "see the components deployed on that management cluster."
    ),
}

_SDDC_LCM_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the SDDC lifecycle managers the fleet manages — the "
        "entry point for 'what SDDCs does this fleet govern'. Each SDDC LCM "
        "is the per-SDDC lifecycle authority the fleet coordinates."
    ),
    "output_shape": (
        "Paged envelope {pageMetadata, sddcLcms: []}. Each SddcLcm carries "
        "fqdn, sddcGroupFqdn, isPrimary (bool), and vspClusters[] (each with "
        "fqdn, id, type). pageMetadata carries totalElements / totalPages so "
        "you can tell whether more pages exist."
    ),
    "next_step": (
        "Pick an SddcLcm's id for fleet-lcm.sddc-lcm.info for full detail, "
        "or cross-reference its vspClusters against fleet-lcm.component.list "
        "(the sddcLcmId field) to see the components under that SDDC."
    ),
}

_SDDC_LCM_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read one SDDC lifecycle manager in full by its id. Requires "
        "sddcLcmId (a UUID) obtained from fleet-lcm.sddc-lcm.list."
    ),
    "output_shape": (
        "SddcLcm object with fqdn, sddcGroupFqdn, isPrimary (bool), and "
        "vspClusters[] (each with fqdn, certificate, id, type)."
    ),
    "next_step": (
        "Cross-reference this SDDC's components via fleet-lcm.component.list "
        "and filter by the sddcLcmId, or check its managed status through "
        "fleet-lcm.component.status on each component."
    ),
}

_COMPONENT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the components the fleet has deployed — the primary "
        "'what is deployed' inventory read. Every managed product node "
        "(Operations, Automation, …) surfaces here with its type, version, "
        "and the SDDC LCM it belongs to."
    ),
    "output_shape": (
        "Object {components: [Component, ...]}. Each Component carries "
        "componentType, fqdn, version, sddcLcmId, deploymentType (e.g. OVA), "
        "nodes[] (size, fqdn, ipAddress, name), and vspCluster. A large "
        "inventory is reduced to a JSONFlux handle."
    ),
    "next_step": (
        "Pick a component's id for fleet-lcm.component.info (full detail) or "
        "fleet-lcm.component.status (is it Running). Group by sddcLcmId to "
        "map components to their SDDC from fleet-lcm.sddc-lcm.list."
    ),
}

_COMPONENT_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read one managed component in full by its id. Requires "
        "componentId (a UUID) obtained from fleet-lcm.component.list. "
        "Returns the same fields as the list plus any per-component detail "
        "the list view omits."
    ),
    "output_shape": (
        "Component object with componentType, fqdn, version, sddcLcmId, "
        "deploymentType, nodes[], and vspCluster."
    ),
    "next_step": (
        "Call fleet-lcm.component.status with the same componentId to confirm "
        "the component is Running, or check fleet-lcm.upgrade-plan.list for "
        "any planned upgrade that targets it."
    ),
}

_COMPONENT_STATUS_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to check the runtime status of one managed component — 'is "
        "component X running'. Requires componentId (a UUID) from "
        "fleet-lcm.component.list."
    ),
    "output_shape": (
        "Object with id (the componentId) and status — one of 'Unknown', "
        "'NotRunning', or 'Running'."
    ),
    "next_step": (
        "If status is NotRunning or Unknown, surface the component's fqdn "
        "(from fleet-lcm.component.info) to the operator and check "
        "fleet-lcm.task.list for a recent failed task on that resource."
    ),
}

_TASK_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the fleet's async operation tasks — 'what is the fleet "
        "doing / what did it just do'. Use to find in-flight or recently "
        "completed upgrades, refreshes, and component actions."
    ),
    "output_shape": (
        "Paged envelope {elements: [TaskSummary, ...], pageMetadata}. Each "
        "TaskSummary carries resourceId, type, createTime / updateTime, "
        "cancellable / retriable (bool), parentTaskId, and a description with "
        "a localizedMessage. pageMetadata.totalElements tells you whether "
        "more pages exist; a large list is reduced to a JSONFlux handle."
    ),
    "next_step": (
        "Pick a task's id for fleet-lcm.task.info for the full task tree and "
        "status. Correlate a task's resourceId with a componentId from "
        "fleet-lcm.component.list to see which component it acted on."
    ),
}

_TASK_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read one async task in full by its id — its status, timing, "
        "and sub-task detail. Requires taskId (a UUID) from "
        "fleet-lcm.task.list, or the upgrade.taskId from "
        "fleet-lcm.system.info."
    ),
    "output_shape": (
        "Task object with resourceId, type, status, createTime / updateTime, "
        "createdBy / updatedBy, cancellable / retriable, parentTaskId, and a "
        "description with a localizedMessage."
    ),
    "next_step": (
        "If the task failed, surface its description.localizedMessage to the "
        "operator. If retriable=true, a retry is available via the fleet's "
        "task-retry action (a write op, outside this read core)."
    ),
}

_UPGRADE_PLAN_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the fleet's upgrade plans — 'what upgrades are planned'. "
        "Each plan pins a desired software set against a component filter."
    ),
    "output_shape": (
        "Paged envelope {elements: [UpgradePlanSummary, ...], pageMetadata}. "
        "Each summary carries id, createTime / updateTime, createdBy, and a "
        "spec with componentsFilter[] and desiredSoftware.components[] "
        "(publicName, type)."
    ),
    "next_step": (
        "Pick a plan's id for fleet-lcm.upgrade-plan.info for the full plan, "
        "or check fleet-lcm.release-version.list to see which target "
        "versions the desiredSoftware could move to."
    ),
}

_UPGRADE_PLAN_INFO_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read one upgrade plan in full by its id. Requires planId "
        "(a UUID) obtained from fleet-lcm.upgrade-plan.list."
    ),
    "output_shape": (
        "UpgradePlan object with id, createTime / updateTime, and a spec "
        "carrying componentsFilter[] and desiredSoftware (the target "
        "component versions)."
    ),
    "next_step": (
        "Cross-reference the plan's componentsFilter ids against "
        "fleet-lcm.component.list to see which deployed components the plan "
        "targets. Applying / precheck-ing a plan is a write op outside this "
        "read core."
    ),
}

_RELEASE_VERSION_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the release versions the fleet can target — 'what can "
        "we upgrade to'. Use when planning an upgrade or confirming a target "
        "version is available."
    ),
    "output_shape": (
        "Paged envelope {elements: [ReleaseVersion, ...], pageMetadata}. Each "
        "ReleaseVersion carries a version string and components[] (publicName, "
        "the available versions[], and type e.g. OPS)."
    ),
    "next_step": (
        "Match a target version's components against a plan's desiredSoftware "
        "in fleet-lcm.upgrade-plan.info, or against the currently-deployed "
        "versions in fleet-lcm.component.list to compute the delta."
    ),
}


#: Per-op ``llm_instructions`` keyed by the dot-form op id, consumed by
#: :mod:`meho_backplane.connectors.fleet_lcm.typed_ops`.
FLEET_LCM_READ_LLM_INSTRUCTIONS: dict[str, dict[str, object]] = {
    "fleet-lcm.health": _HEALTH_INSTRUCTIONS,
    "fleet-lcm.system.info": _SYSTEM_INFO_INSTRUCTIONS,
    "fleet-lcm.config.info": _CONFIG_INFO_INSTRUCTIONS,
    "fleet-lcm.sddc-lcm.list": _SDDC_LCM_LIST_INSTRUCTIONS,
    "fleet-lcm.sddc-lcm.info": _SDDC_LCM_INFO_INSTRUCTIONS,
    "fleet-lcm.component.list": _COMPONENT_LIST_INSTRUCTIONS,
    "fleet-lcm.component.info": _COMPONENT_INFO_INSTRUCTIONS,
    "fleet-lcm.component.status": _COMPONENT_STATUS_INSTRUCTIONS,
    "fleet-lcm.task.list": _TASK_LIST_INSTRUCTIONS,
    "fleet-lcm.task.info": _TASK_INFO_INSTRUCTIONS,
    "fleet-lcm.upgrade-plan.list": _UPGRADE_PLAN_LIST_INSTRUCTIONS,
    "fleet-lcm.upgrade-plan.info": _UPGRADE_PLAN_INFO_INSTRUCTIONS,
    "fleet-lcm.release-version.list": _RELEASE_VERSION_LIST_INSTRUCTIONS,
}

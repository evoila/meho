# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the vSAN cluster operations of #4446.

``_register.py`` splats :data:`VSAN_CLUSTER_COMPOSITES` into its
``_COMPOSITES`` table and maps :data:`VSAN_CLUSTER_GROUP_KEY` to
:data:`VSAN_CLUSTER_WHEN_TO_USE`; ``_governed_subops.py`` reads
:data:`VSAN_CLUSTER_GOVERNED_SUBOPS`. The read is ``safe``; every change is
``requires_approval=True``. Importing this module registers the approval
previews of the changes.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_checks,
    _vsan_common,
    _vsan_hcl,
    _vsan_power,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_checks_schemas import (
    OBJECT_REPAIR_LLM_INSTRUCTIONS,
    OBJECT_REPAIR_PARAMETER_SCHEMA,
    OBJECT_REPAIR_RESPONSE_SCHEMA,
    PROACTIVE_TEST_LLM_INSTRUCTIONS,
    PROACTIVE_TEST_PARAMETER_SCHEMA,
    PROACTIVE_TEST_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_common import vsan_op_id
from meho_backplane.connectors.vmware_rest.composites._vsan_schemas import (
    HCL_UPDATE_LLM_INSTRUCTIONS,
    HCL_UPDATE_PARAMETER_SCHEMA,
    HCL_UPDATE_RESPONSE_SCHEMA,
    POWER_ACTION_RESPONSE_SCHEMA,
    POWER_PRECHECK_LLM_INSTRUCTIONS,
    POWER_PRECHECK_PARAMETER_SCHEMA,
    POWER_PRECHECK_RESPONSE_SCHEMA,
    SHUTDOWN_LLM_INSTRUCTIONS,
    SHUTDOWN_PARAMETER_SCHEMA,
    START_LLM_INSTRUCTIONS,
    START_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    VSAN_QUERY_HEALTH_SUMMARY,
)
from meho_backplane.operations._preview import register_preview_builder

__all__ = [
    "VSAN_CLUSTER_COMPOSITES",
    "VSAN_CLUSTER_GOVERNED_SUBOPS",
    "VSAN_CLUSTER_GROUP_KEY",
    "VSAN_CLUSTER_WHEN_TO_USE",
]

VSAN_CLUSTER_GROUP_KEY: Final = "vsan_cluster"

VSAN_CLUSTER_WHEN_TO_USE: Final = (
    "Use for work on a whole vSAN cluster and its vSAN data. One read: "
    "vsan.cluster.power.precheck (can the cluster be shut down now, what blocks it, and its "
    "power state). Changes, each waits for approval: vsan.hcl.update (replace the HCL "
    "database and the release catalog, from the web or from a datastore file), "
    "vsan.cluster.shutdown and vsan.cluster.start (shut down or start every host of the "
    "cluster; they answer accepted with a vCenter task id, followed with vmware.task.info), "
    "vsan.proactive_test.run (VM creation, network or storage test; the last two put load "
    "on the cluster) and vsan.object.repair (repair objects now)."
)

_TAGS: Final = ["composite", "vsan", "vi-json"]

VSAN_CLUSTER_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": _vsan_power.POWER_PRECHECK_OP_ID,
        "handler": _vsan_power.vsan_cluster_power_precheck_composite,
        "summary": "Can a whole vSAN cluster be shut down now, and what blocks it.",
        "description": (
            "Runs the vSAN checks for a cluster shut down and reads the power state of the "
            "cluster and its last power task. Red checks block the shut down; yellow ones "
            "warn. Read-only."
        ),
        "parameter_schema": POWER_PRECHECK_PARAMETER_SCHEMA,
        "response_schema": POWER_PRECHECK_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "read-only", "power"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": POWER_PRECHECK_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vsan_hcl.HCL_UPDATE_OP_ID,
        "handler": _vsan_hcl.vsan_hcl_update_composite,
        "summary": "Replace the vSAN HCL database and the release catalog.",
        "description": (
            "Replaces the vSAN HCL database (vCenter downloads it, or MEHO sends a datastore "
            "file) and the release catalog (a datastore file). Steps run in order; a failed "
            "step is reported with the steps that ran and did not run."
        ),
        "parameter_schema": HCL_UPDATE_PARAMETER_SCHEMA,
        "response_schema": HCL_UPDATE_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "write", "hcl"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": HCL_UPDATE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vsan_power.SHUTDOWN_OP_ID,
        "handler": _vsan_power.vsan_cluster_shutdown_composite,
        "summary": "Shut down every host of a vSAN cluster for planned work.",
        "description": (
            "Starts the vSAN cluster shut down: vSAN stops the VMs and the hosts in order. "
            "Answers accepted with the vCenter task id at once; follow it with "
            "vmware.task.info. Starts nothing when a power task still runs."
        ),
        "parameter_schema": SHUTDOWN_PARAMETER_SCHEMA,
        "response_schema": POWER_ACTION_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "write", "power"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": SHUTDOWN_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vsan_power.START_OP_ID,
        "handler": _vsan_power.vsan_cluster_start_composite,
        "summary": "Start a vSAN cluster again after a cluster shut down.",
        "description": (
            "Starts the vSAN cluster start: vSAN brings the hosts back into the cluster and "
            "starts the VMs it stopped. Answers accepted with the vCenter task id at once; "
            "follow it with vmware.task.info."
        ),
        "parameter_schema": START_PARAMETER_SCHEMA,
        "response_schema": POWER_ACTION_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "write", "power"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": START_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vsan_checks.PROACTIVE_TEST_OP_ID,
        "handler": _vsan_checks.vsan_proactive_test_composite,
        "summary": "Run a vSAN proactive test: VM creation, network or storage load.",
        "description": (
            "Runs one vSAN proactive test on a cluster and returns the result per host. The "
            "network and storage tests put real load on the cluster."
        ),
        "parameter_schema": PROACTIVE_TEST_PARAMETER_SCHEMA,
        "response_schema": PROACTIVE_TEST_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "write", "test"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": PROACTIVE_TEST_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vsan_checks.OBJECT_REPAIR_OP_ID,
        "handler": _vsan_checks.vsan_object_repair_composite,
        "summary": "Repair vSAN objects with reduced redundancy now.",
        "description": (
            "Puts the vSAN objects with reduced redundancy (all, or the given UUIDs) into the "
            "repair queue now, without waiting for the vSAN repair timer. The repair adds "
            "disk and network load."
        ),
        "parameter_schema": OBJECT_REPAIR_PARAMETER_SCHEMA,
        "response_schema": OBJECT_REPAIR_RESPONSE_SCHEMA,
        "group_key": VSAN_CLUSTER_GROUP_KEY,
        "tags": [*_TAGS, "write", "repair"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": OBJECT_REPAIR_LLM_INSTRUCTIONS,
    },
)

_READ_SUBOPS: Final = (
    "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    vsan_op_id(_vsan_common.VSAN_QUERY_CLUSTER_POWER_CONTEXT),
)

#: composite op id -> the vendor calls it may make. The change is listed with
#: the reads it needs, so the reconcile lanes and the grant discovery see all.
VSAN_CLUSTER_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _vsan_hcl.HCL_UPDATE_OP_ID: (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        vsan_op_id(_vsan_common.VSAN_UPDATE_HCL_DB_FROM_WEB),
        vsan_op_id(_vsan_common.VSAN_UPLOAD_HCL_DB),
        vsan_op_id(_vsan_common.VSAN_UPLOAD_RELEASE_DB),
    ),
    _vsan_power.SHUTDOWN_OP_ID: (
        *_READ_SUBOPS,
        vsan_op_id(VSAN_QUERY_HEALTH_SUMMARY),
        vsan_op_id(_vsan_common.VSAN_PERFORM_CLUSTER_POWER_ACTION),
    ),
    _vsan_power.START_OP_ID: (
        *_READ_SUBOPS,
        vsan_op_id(_vsan_common.VSAN_PERFORM_CLUSTER_POWER_ACTION),
    ),
    _vsan_checks.PROACTIVE_TEST_OP_ID: (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        vsan_op_id(_vsan_common.VSAN_CREATE_VM_TEST),
        vsan_op_id(_vsan_common.VSAN_NETWORK_PERF_TEST),
        vsan_op_id(_vsan_common.VSAN_VMDK_WORKLOAD_TYPES),
        vsan_op_id(_vsan_common.VSAN_RUN_VMDK_LOAD_TEST),
    ),
    _vsan_checks.OBJECT_REPAIR_OP_ID: (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        vsan_op_id(_vsan_common.VSAN_REPAIR_OBJECTS),
    ),
}

register_preview_builder(_vsan_hcl.HCL_UPDATE_OP_ID, _vsan_hcl.hcl_update_preview)
register_preview_builder(_vsan_power.SHUTDOWN_OP_ID, _vsan_power.cluster_power_preview)
register_preview_builder(_vsan_power.START_OP_ID, _vsan_power.cluster_power_preview)
register_preview_builder(_vsan_checks.PROACTIVE_TEST_OP_ID, _vsan_checks.checks_preview)
register_preview_builder(_vsan_checks.OBJECT_REPAIR_OP_ID, _vsan_checks.checks_preview)

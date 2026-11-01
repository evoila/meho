# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSphere Supervisor: enable, disable and status.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor import (
    supervisor_disable_composite,
    supervisor_enable_composite,
    supervisor_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _supervisor_disable_preview,
    _supervisor_enable_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
    SUPERVISOR_DISABLE_RESPONSE_SCHEMA,
    SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
    SUPERVISOR_ENABLE_RESPONSE_SCHEMA,
    SUPERVISOR_STATUS_PARAMETER_SCHEMA,
    SUPERVISOR_STATUS_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]


#: Why the Supervisor and namespace composites stay off the vmware-rest-8.0
#: catalog.
SUPERVISOR_9X_REASON: Final = (
    "built and checked against the vSphere 9.x Supervisor and namespace API "
    "(the 9.x enable path, the 9.x network type enums and the namespaces v2 "
    "instances API); the vCenter 8.0 shape of this API is not checked yet"
)

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.supervisor.enable",
        handler=supervisor_enable_composite,
        summary="Turn on a vSphere Supervisor (Kubernetes control plane) on one cluster.",
        description=(
            "Turns on a vSphere Supervisor on one cluster with the vCenter enable spec "
            "(control_plane, workloads, optional zone). An unknown network type or edge "
            "provider is refused before any change. It returns the new Supervisor id at "
            "once; poll supervisor.status until it is ready."
        ),
        parameter_schema=SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_ENABLE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "supervisor", "wcp"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.enable"],
        preview=_supervisor_enable_preview,
        not_on_8_0=SUPERVISOR_9X_REASON,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.supervisor.disable",
        handler=supervisor_disable_composite,
        summary="Remove the vSphere Supervisor from a cluster.",
        description=(
            "Removes the Supervisor from a cluster: its control plane VMs and worker nodes "
            "go away, the cluster's networking and zone stay. It returns at once "
            "(status disabling); poll supervisor.status, which shows REMOVING while it runs."
        ),
        parameter_schema=SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_DISABLE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "supervisor", "wcp"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.disable"],
        preview=_supervisor_disable_preview,
        not_on_8_0=SUPERVISOR_9X_REASON,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.supervisor.status",
        handler=supervisor_status_composite,
        summary="Read the Supervisor state of a cluster (config and Kubernetes status, ready).",
        description=(
            "Reads the Supervisor of a cluster: config_status, kubernetes_status, a ready "
            "flag (RUNNING and READY) and the latest messages and conditions. Use it to poll "
            "after supervisor.enable or supervisor.disable. Read-only."
        ),
        parameter_schema=SUPERVISOR_STATUS_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_STATUS_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "read-only", "namespace-management", "supervisor", "wcp"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.status"],
        not_on_8_0=SUPERVISOR_9X_REASON,
        classifier_other=True,
    ),
)

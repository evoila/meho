# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host writes with a VM move: evacuate, leave maintenance mode, detach from a distributed switch.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    host_detach_from_vds_composite,
    host_evacuate_composite,
    host_maintenance_exit_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _host_detach_from_vds_preview,
    _host_evacuate_preview,
    _host_maintenance_exit_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
    HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
    HOST_EVACUATE_PARAMETER_SCHEMA,
    HOST_EVACUATE_RESPONSE_SCHEMA,
    HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA,
    HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.host.evacuate",
        handler=host_evacuate_composite,
        summary="Move every running VM off a host, then put the host into maintenance mode.",
        description=(
            "With DRS fully automated, vCenter moves the running VMs as part of the "
            "maintenance-mode request. Otherwise MEHO moves each running VM where DRS recommends "
            "(vm.migrate) and stops before maintenance mode if one cannot move, unless "
            "tolerate_partial_failure is set. vsan_mode sets the vSAN data option."
        ),
        parameter_schema=HOST_EVACUATE_PARAMETER_SCHEMA,
        response_schema=HOST_EVACUATE_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "maintenance", "recursive"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.evacuate"],
        preview=_host_evacuate_preview,
        governed_subops=_write._SUB_OPS_HOST_EVACUATE + _write._VIM_SUB_OPS_HOST_EVACUATE,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.host.maintenance.exit",
        handler=host_maintenance_exit_composite,
        summary="Take a host out of maintenance mode, after a second person approves.",
        description=(
            "Reads the host first: a host that is not connected is refused, and a host that "
            "is not in maintenance mode answers unchanged with nothing sent. Then sends the "
            "exit request and waits for its task. Returns the host's maintenance state, "
            "connection state and vSphere HA state afterwards."
        ),
        parameter_schema=HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA,
        response_schema=HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "maintenance", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.maintenance.exit"],
        preview=_host_maintenance_exit_preview,
        governed_subops=_write._VIM_SUB_OPS_HOST_MAINTENANCE_EXIT,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.host.detach_from_vds",
        handler=host_detach_from_vds_composite,
        summary="Move the VM NICs on a host to a standard network, then take the host off a DVS.",
        description=(
            "Moves every NIC of every VM on the host to fallback_network, then removes the host "
            "from the distributed switch. If a VM could not be moved, the host stays on the "
            "switch (status incomplete)."
        ),
        parameter_schema=HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
        response_schema=HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "networking"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.detach_from_vds"],
        preview=_host_detach_from_vds_preview,
        governed_subops=_write._SUB_OPS_HOST_DETACH_FROM_VDS
        + _write._VIM_SUB_OPS_HOST_DETACH_FROM_VDS,
        classifier_other=True,
    ),
)

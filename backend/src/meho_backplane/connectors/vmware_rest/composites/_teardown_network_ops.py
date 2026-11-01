# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Port group deletes: a distributed port group and a standard port group.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_network import (
    host_standard_portgroup_delete_composite,
    host_standard_portgroup_delete_preview,
    network_portgroup_delete_composite,
    network_portgroup_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.network.portgroup.delete",
        handler=network_portgroup_delete_composite,
        summary="Delete a distributed portgroup. Refused while a VM or adapter uses it.",
        description=(
            "Deletes one distributed portgroup (vSphere Destroy_Task) and reads it again to "
            "check it is gone. Refused before any change while a VM, VMkernel adapter or other "
            "port uses it, or if it is the uplink portgroup or backed by NSX. A second person "
            "must always approve it."
        ),
        parameter_schema=NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.delete"
        ],
        preview=network_portgroup_delete_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.host.standard_portgroup.delete",
        handler=host_standard_portgroup_delete_composite,
        summary="Remove a port group from one host's standard switch. Refused while used.",
        description=(
            "Removes one port group from the standard switch of one host (vSphere "
            "RemovePortGroup), then reads the host again. Refused before any change while a VM "
            "on the host (also a powered-off one) or a VMkernel adapter uses it. Other hosts "
            "are not touched. A second person must approve."
        ),
        parameter_schema=HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=[
            "composite",
            "write",
            "networking",
            "host",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.host.standard_portgroup.delete"
        ],
        preview=host_standard_portgroup_delete_preview,
    ),
)

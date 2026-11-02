# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Distributed port group writes: create, set the VLAN, set the security policy.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    network_portgroup_create_composite,
    network_portgroup_security_set_composite,
    network_portgroup_vlan_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_VLAN_SET_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.network.portgroup.create",
        handler=network_portgroup_create_composite,
        summary="Create a distributed portgroup on a switch, with a VLAN trunk or one access VLAN.",
        description=(
            "Creates a distributed portgroup on an existing distributed switch (vSphere "
            "CreateDVPortgroup_Task), waits for the task and reads the new portgroup back. Give "
            "vlan_trunk_ranges or vlan_id, not both; give neither to keep the switch default."
        ),
        parameter_schema=NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.create"
        ],
        governed_subops=_write._VIM_SUB_OPS_NETWORK_PORTGROUP_CREATE,
        no_preview=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.network.portgroup.security.set",
        handler=network_portgroup_security_set_composite,
        summary="Set promiscuous mode, forged transmits and MAC changes on a portgroup.",
        description=(
            "Sets promiscuous mode, forged transmits and MAC address changes on a distributed "
            "portgroup (vSphere ReconfigureDVPortgroup_Task). Only the values you give change. "
            "It reads the policy before and after, so the approver sees both."
        ),
        parameter_schema=NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.security.set"
        ],
        governed_subops=_write._VIM_SUB_OPS_NETWORK_PORTGROUP_SECURITY_SET,
        no_preview=True,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.network.portgroup.vlan.set",
        handler=network_portgroup_vlan_set_composite,
        summary="Change the VLAN of a distributed portgroup: trunk ranges or one access VLAN.",
        description=(
            "Sets the VLAN of an existing distributed portgroup (vSphere "
            "ReconfigureDVPortgroup_Task). The new VLAN replaces the old one; there is no "
            "merge. Give exactly one of vlan_trunk_ranges or vlan_id. When the VLAN already "
            "matches, nothing is written."
        ),
        parameter_schema=NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_VLAN_SET_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.vlan.set"
        ],
        governed_subops=_write._VIM_SUB_OPS_NETWORK_PORTGROUP_VLAN_SET,
        no_preview=True,
        classifier_other=True,
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DRS group members change.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _drs_group
from meho_backplane.connectors.vmware_rest.composites._drs_group import (
    cluster_drs_group_members_set_composite,
    cluster_drs_group_members_set_preview,
)
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA,
    CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.cluster.drs_group.members.set",
        handler=cluster_drs_group_members_set_composite,
        summary="Add VMs or hosts to, or remove them from, an existing DRS group.",
        description=(
            "Adds VMs to, or removes VMs from, an existing DRS VM group, by name. Does the same "
            "for hosts in a host group. The approval card shows what changes and which rules use "
            "the group. Like 'govc cluster.group.change'."
        ),
        parameter_schema=CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS[
            "vmware.composite.cluster.drs_group.members.set"
        ],
        preview=cluster_drs_group_members_set_preview,
        governed_subops=_drs_group._VIM_SUB_OPS_CLUSTER_DRS_GROUP_MEMBERS_SET,
        classifier_other=True,
    ),
)

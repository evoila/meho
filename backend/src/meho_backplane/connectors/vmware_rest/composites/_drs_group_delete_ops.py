# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DRS group delete.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _drs_group_delete
from meho_backplane.connectors.vmware_rest.composites._drs_group_delete import (
    cluster_drs_group_delete_composite,
    cluster_drs_group_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_GROUP_DELETE_PARAMETER_SCHEMA,
    CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.cluster.drs_group.delete",
        handler=cluster_drs_group_delete_composite,
        summary="Delete one DRS VM group or host group from a cluster, by name.",
        description=(
            "Deletes one DRS VM group or host group, found by name. The VMs and hosts stay. "
            "Refused while a rule still uses the group. Destructive: a second person must always "
            "approve it. Like 'govc cluster.group.remove'."
        ),
        parameter_schema=CLUSTER_DRS_GROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json", "teardown", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.drs_group.delete"],
        preview=cluster_drs_group_delete_preview,
        governed_subops=_drs_group_delete._VIM_SUB_OPS_CLUSTER_DRS_GROUP_DELETE,
    ),
)

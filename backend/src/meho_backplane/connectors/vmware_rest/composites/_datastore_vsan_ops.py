# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vSAN datastore folder read.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._datastore_vsan import (
    datastore_vsan_folder_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
    DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.datastore.vsan.folder.list",
        handler=datastore_vsan_folder_list_composite,
        summary="List the folders of a vSAN datastore, or the files in one folder.",
        description=(
            "Lists the top-level folders of a vSAN datastore with friendly name, UUID path, "
            "size and the registered VMs that use each. With folder, lists the files in that "
            "folder with size and last change; a missing folder answers 'absent'. Read-only."
        ),
        parameter_schema=DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
        response_schema=DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "vsan", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.datastore.vsan.folder.list"
        ],
    ),
)

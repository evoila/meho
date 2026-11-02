# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The datastore file list read.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._datastore_list import (
    datastore_file_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
    DATASTORE_FILE_LIST_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.datastore.file.list",
        handler=datastore_file_list_composite,
        summary="List the files in one datastore folder, with size and last change.",
        description=(
            "Lists the files and folders in one datastore folder (SearchDatastore_Task), by "
            "datastore name and a path from the root. pattern keeps matching file names. "
            "include_usage adds folder sizes and the VMs that use each entry. Works on "
            "vCenter and on a standalone ESXi host. Read-only."
        ),
        parameter_schema=DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
        response_schema=DATASTORE_FILE_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.file.list"],
    ),
)

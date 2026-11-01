# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Datastore files: create a folder and delete a file or folder.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._datastore_files import (
    datastore_dir_create_composite,
    datastore_dir_create_preview,
    datastore_file_delete_composite,
    datastore_file_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
    DATASTORE_DIR_CREATE_RESPONSE_SCHEMA,
    DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
    DATASTORE_FILE_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.datastore.file.delete",
        handler=datastore_file_delete_composite,
        summary="Delete a file or folder on a datastore. Refused for files of registered VMs.",
        description=(
            "Deletes one file, or one folder with all it holds, on a VMFS or NFS datastore "
            "(vSphere DeleteDatastoreFile_Task). Refused before any change for unsafe paths and "
            "for files a registered VM uses (listed in blockers). A second person must always "
            "approve it, after seeing the size."
        ),
        parameter_schema=DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_FILE_DELETE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=[
            "composite",
            "write",
            "storage",
            "datastore",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.file.delete"],
        preview=datastore_file_delete_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.datastore.dir.create",
        handler=datastore_dir_create_composite,
        summary="Create a folder on a datastore. Does nothing if it already exists.",
        description=(
            "Creates one folder on a VMFS or NFS datastore (vSphere FileManager.MakeDirectory), "
            "then checks that it exists. Same path rules as datastore.file.delete. A folder "
            "that already exists gives unchanged. A missing parent is refused unless "
            "create_parents is true."
        ),
        parameter_schema=DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_DIR_CREATE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "datastore", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.dir.create"],
        preview=datastore_dir_create_preview,
    ),
)

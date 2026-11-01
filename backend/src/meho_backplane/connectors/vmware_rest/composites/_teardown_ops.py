# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Folder delete.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    folder_delete_composite,
    folder_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    FOLDER_DELETE_PARAMETER_SCHEMA,
    FOLDER_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.folder.delete",
        handler=folder_delete_composite,
        summary="Delete an EMPTY inventory folder. Folders with content are refused.",
        description=(
            "Deletes one EMPTY inventory folder. A folder with content is refused "
            "and its content is listed. Root and datacenter folders are refused "
            "too. Destructive: a second person must always approve it."
        ),
        parameter_schema=FOLDER_DELETE_PARAMETER_SCHEMA,
        response_schema=FOLDER_DELETE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=[
            "composite",
            "write",
            "vm",
            "inventory",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.folder.delete"],
        preview=folder_delete_preview,
    ),
)

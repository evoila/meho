# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Content library deletes: a library and an item.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._library_delete import (
    content_library_delete_composite,
    content_library_delete_preview,
    content_library_item_delete_composite,
    content_library_item_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.content_library.delete",
        handler=content_library_delete_composite,
        summary="Delete a content library (local or subscribed).",
        description=(
            "Deletes one content library, found by id or name. Refused before any change "
            "while another library subscribes to it, a VM has one of its items in a CD-ROM "
            "or floppy drive, or it has items and delete_items is not true. A second person "
            "must always approve."
        ),
        parameter_schema=CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "write", "content-library", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.delete"
        ],
        preview=content_library_delete_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.content_library.item.delete",
        handler=content_library_item_delete_composite,
        summary="Delete one item of a local content library.",
        description=(
            "Deletes one item of a local content library, found by id or by name in a "
            "library. Refused before any change if the library is not local, or while a VM "
            "has the item in a CD-ROM or floppy drive. A second person must always approve."
        ),
        parameter_schema=CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "write", "content-library", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.item.delete"
        ],
        preview=content_library_item_delete_preview,
    ),
)

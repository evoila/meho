# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Subscribed content library: create, sync, status and the item list.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._library import (
    content_library_subscribed_create_composite,
    content_library_subscribed_items_list_composite,
    content_library_subscribed_status_composite,
    content_library_subscribed_sync_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _content_library_subscribed_create_preview,
    _content_library_subscribed_sync_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.create",
        handler=content_library_subscribed_create_composite,
        summary="Create a subscribed content library, e.g. the Kubernetes release library.",
        description=(
            "Creates a content library on a datastore that copies a published library from "
            "subscription_url, for example the Kubernetes releases a Supervisor uses. It "
            "returns the new library id; vCenter syncs in the background. A BASIC password is "
            "never shown in a preview, broadcast or audit row."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "content-library", "subscribed", "tkr", "vks"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.create"
        ],
        preview=_content_library_subscribed_create_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.sync",
        handler=content_library_subscribed_sync_composite,
        summary="Start a sync of a subscribed content library now.",
        description=(
            "Finds the subscribed library by id or name and tells vCenter to sync it with "
            "the publisher now. It returns as soon as the sync starts; with on_demand only "
            "the item list syncs. Check the result with content_library.subscribed.status."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "content-library", "subscribed", "sync"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.sync"
        ],
        preview=_content_library_subscribed_sync_preview,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.status",
        handler=content_library_subscribed_status_composite,
        summary="Read a subscribed content library: its subscription and when it last synced.",
        description=(
            "Finds the subscribed library by id or name and reads its name, subscription "
            "URL, sync settings and last_sync_time. last_sync_time is set once the first "
            "sync is done. vCenter does not return the password. Read-only."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "read-only", "content-library", "subscribed"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.status"
        ],
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.items.list",
        handler=content_library_subscribed_items_list_composite,
        summary="List the items of a subscribed content library, e.g. Kubernetes releases.",
        description=(
            "Finds the subscribed library by id or name and lists its items: name (for a "
            "Kubernetes release library, the release version), type, version, cached (files "
            "downloaded), size and last sync. A long list comes back as a result handle. "
            "Read-only."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "read-only", "content-library", "subscribed", "tkr"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.items.list"
        ],
    ),
)

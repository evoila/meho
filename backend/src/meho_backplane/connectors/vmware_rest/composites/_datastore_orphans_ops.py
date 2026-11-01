# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The datastore orphan folders read.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._datastore_orphans import (
    datastore_orphan_dirs_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA,
    DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.datastore.orphan_dirs",
        handler=datastore_orphan_dirs_composite,
        summary="Find the top-level datastore folders that no registered VM uses.",
        description=(
            "Checks every datastore of a vCenter (or the named ones) in one call. One row per "
            "top-level folder with size, file count, latest change, the VMs that use it and a "
            "classification: orphan, system or claimed. Datastores that cannot be read are "
            "listed in skipped. vCenter only. Read-only."
        ),
        parameter_schema=DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA,
        response_schema=DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "cleanup", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.orphan_dirs"],
    ),
)

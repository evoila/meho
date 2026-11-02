# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The migration inventory read.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._migration_inventory import (
    migration_inventory_composite,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_schemas import (
    MIGRATION_INVENTORY_DESCRIPTION,
    MIGRATION_INVENTORY_LLM_INSTRUCTIONS,
    MIGRATION_INVENTORY_OP_ID,
    MIGRATION_INVENTORY_PARAMETER_SCHEMA,
    MIGRATION_INVENTORY_RESPONSE_SCHEMA,
    MIGRATION_INVENTORY_SUMMARY,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=MIGRATION_INVENTORY_OP_ID,
        handler=migration_inventory_composite,
        summary=MIGRATION_INVENTORY_SUMMARY,
        description=MIGRATION_INVENTORY_DESCRIPTION,
        parameter_schema=MIGRATION_INVENTORY_PARAMETER_SCHEMA,
        response_schema=MIGRATION_INVENTORY_RESPONSE_SCHEMA,
        group_key="migration",
        tags=["composite", "read-only", "migration", "inventory", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=MIGRATION_INVENTORY_LLM_INSTRUCTIONS,
        classifier_other=True,
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Licence assign.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._license import (
    LICENSE_ASSIGN_LLM_INSTRUCTIONS,
    LICENSE_ASSIGN_PARAMETER_SCHEMA,
    LICENSE_ASSIGN_RESPONSE_SCHEMA,
    license_assign_composite,
    license_assign_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.license_common import (
    LICENSE_GROUP_KEY,
    LICENSE_KEY_TAG,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.license.assign",
        handler=license_assign_composite,
        summary="Assign a licence from the vCenter's licence inventory to one host or the vCenter.",
        description=(
            "Assigns one licence, named by its name or the last 5 characters of its key, "
            "to one asset: a host or the vCenter. MEHO looks up the full key itself and "
            "never shows it. Waits until a second person approves."
        ),
        parameter_schema=LICENSE_ASSIGN_PARAMETER_SCHEMA,
        response_schema=LICENSE_ASSIGN_RESPONSE_SCHEMA,
        group_key=LICENSE_GROUP_KEY,
        tags=["composite", "write", "license", LICENSE_KEY_TAG, "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=LICENSE_ASSIGN_LLM_INSTRUCTIONS,
        preview=license_assign_preview,
    ),
)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Reduce Data Node - SpecialistAgent implementation.

This is step 5 of the deterministic workflow.
If call_operation returned data_available=False, this node fetches the cached
data and optionally aggregates it via JSONFlux.

Inherits all behavior from BaseReduceDataNode.
"""

from __future__ import annotations

from dataclasses import dataclass

from meho_app.modules.agents.base.reduce_data import BaseReduceDataNode


@dataclass
class ReduceDataNode(BaseReduceDataNode):
    """SpecialistAgent reduce data node.

    Inherits all behavior from BaseReduceDataNode.

    Emits:
        action: reduce_data with table and row_count
        observation: result_type "raw" (full data) and "aggregated" (markdown)
    """

    pass

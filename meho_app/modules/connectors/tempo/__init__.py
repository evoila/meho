# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Typed connector for Tempo HTTP API with pre-defined operations
for distributed trace search, service graph, and tag discovery.
No topology entities -- Tempo is query-only.
"""

from meho_app.modules.connectors.tempo.connector import TempoConnector
from meho_app.modules.connectors.tempo.operations import TEMPO_OPERATIONS, TEMPO_OPERATIONS_VERSION

__all__ = [
    "TEMPO_OPERATIONS",
    "TEMPO_OPERATIONS_VERSION",
    "TempoConnector",
]

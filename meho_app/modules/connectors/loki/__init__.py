# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Connector.

Typed connector for Loki HTTP API with pre-defined operations
for log search, error investigation, volume analysis, and label discovery.
No topology entities -- Loki is query-only.
"""

from meho_app.modules.connectors.loki.connector import LokiConnector
from meho_app.modules.connectors.loki.operations import LOKI_OPERATIONS, LOKI_OPERATIONS_VERSION

__all__ = [
    "LOKI_OPERATIONS",
    "LOKI_OPERATIONS_VERSION",
    "LokiConnector",
]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Status Operations.

Cluster health and receiver introspection.
"""

from meho_app.modules.connectors.base import OperationDefinition

STATUS_OPERATIONS = [
    OperationDefinition(
        operation_id="get_cluster_status",
        name="Get Cluster Status",
        description="Alertmanager cluster health: cluster name, peer count, per-peer details "
        "(name, address, state), HA readiness.",
        category="status",
        parameters=[],
        example="get_cluster_status()",
    ),
    OperationDefinition(
        operation_id="list_receivers",
        name="List Receivers",
        description="List configured notification receivers. Returns receiver names.",
        category="status",
        parameters=[],
        example="list_receivers()",
    ),
]

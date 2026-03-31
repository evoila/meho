# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Discovery Operations.

Label listing and label value enumeration for log filtering.
"""

from meho_app.modules.connectors.base import OperationDefinition

DISCOVERY_OPERATIONS = [
    OperationDefinition(
        operation_id="list_labels",
        name="List Log Labels",
        description="Discover available log labels in Loki. Returns label names that can be used "
        "for filtering in other operations. Common labels: namespace, pod, container, "
        "service_name, level/severity, job.",
        category="discovery",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to search for labels (e.g., '1h', '6h'). Default: 1h",
            },
        ],
        example="list_labels()",
    ),
    OperationDefinition(
        operation_id="list_label_values",
        name="List Label Values",
        description="Get all values for a specific log label. Use after list_labels to discover "
        "what namespaces, pods, or services are available.",
        category="discovery",
        parameters=[
            {
                "name": "label",
                "type": "string",
                "required": True,
                "description": "Label name to get values for (e.g., 'namespace', 'pod', 'service_name')",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to search for values (e.g., '1h', '6h'). Default: 1h",
            },
        ],
        example="list_label_values(label='namespace')",
    ),
]

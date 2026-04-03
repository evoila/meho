# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Type Definitions.

Entity types that Prometheus can discover and expose to the agent.
ScrapeTarget is the sole entity type -- metrics are operation attributes,
not topology entities.
"""

from meho_app.modules.connectors.base import TypeDefinition

PROMETHEUS_TYPES = [
    TypeDefinition(
        type_name="ScrapeTarget",
        description="A Prometheus scrape target endpoint being monitored. Represents a concrete "
        "service or node that Prometheus actively scrapes metrics from.",
        category="monitoring",
        properties=[
            {
                "name": "job",
                "type": "string",
                "description": "Scrape job name (e.g., 'kubernetes-pods', 'node-exporter')",
            },
            {
                "name": "instance",
                "type": "string",
                "description": "Target instance address (host:port)",
            },
            {
                "name": "health",
                "type": "string",
                "description": "Target health: up, down, or unknown",
            },
            {
                "name": "labels_url",
                "type": "string",
                "description": "URL to target labels endpoint",
            },
            {
                "name": "namespace",
                "type": "string",
                "description": "Kubernetes namespace (if discovered via K8s SD)",
                "required": False,
            },
            {
                "name": "pod",
                "type": "string",
                "description": "Kubernetes pod name (if discovered via K8s SD)",
                "required": False,
            },
            {
                "name": "node",
                "type": "string",
                "description": "Kubernetes node name (if discovered via K8s SD)",
                "required": False,
            },
        ],
    ),
]

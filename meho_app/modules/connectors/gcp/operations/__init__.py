# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Operation Definitions (TASK-102)

Aggregates all operation definitions from category-specific modules.
"""

from meho_app.modules.connectors.gcp.operations.artifact_registry import (
    ARTIFACT_REGISTRY_OPERATIONS,
)
from meho_app.modules.connectors.gcp.operations.cloud_build import CLOUD_BUILD_OPERATIONS
from meho_app.modules.connectors.gcp.operations.compute import COMPUTE_OPERATIONS
from meho_app.modules.connectors.gcp.operations.gke import GKE_OPERATIONS
from meho_app.modules.connectors.gcp.operations.monitoring import MONITORING_OPERATIONS
from meho_app.modules.connectors.gcp.operations.network import NETWORK_OPERATIONS

# Version for tracking operation updates
GCP_OPERATIONS_VERSION = "2026.03.09.2"  # Phase 49: Artifact Registry operations added

# Aggregate all operations
GCP_OPERATIONS = (
    COMPUTE_OPERATIONS
    + GKE_OPERATIONS
    + NETWORK_OPERATIONS
    + MONITORING_OPERATIONS
    + CLOUD_BUILD_OPERATIONS
    + ARTIFACT_REGISTRY_OPERATIONS
)

__all__ = [
    "ARTIFACT_REGISTRY_OPERATIONS",
    "CLOUD_BUILD_OPERATIONS",
    "COMPUTE_OPERATIONS",
    "GCP_OPERATIONS",
    "GCP_OPERATIONS_VERSION",
    "GKE_OPERATIONS",
    "MONITORING_OPERATIONS",
    "NETWORK_OPERATIONS",
]

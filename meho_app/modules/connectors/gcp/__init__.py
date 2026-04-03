# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Connector Module (TASK-102)

Provides native Google Cloud Platform integration using official SDKs.

Supported services:
- Compute Engine: VMs, disks, snapshots
- GKE: Clusters, node pools
- Networking: VPCs, subnets, firewalls
- Cloud Monitoring: Metrics, alerts
"""

from meho_app.modules.connectors.gcp.connector import GCPConnector
from meho_app.modules.connectors.gcp.operations import GCP_OPERATIONS, GCP_OPERATIONS_VERSION
from meho_app.modules.connectors.gcp.types import GCP_TYPES

__all__ = [
    "GCP_OPERATIONS",
    "GCP_OPERATIONS_VERSION",
    "GCP_TYPES",
    "GCPConnector",
]

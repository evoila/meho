# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Connector Module (Phase 92).

Provides native Azure cloud integration using official async SDKs.

Supported services:
- Compute: VMs, managed disks
- Monitor: Metrics, alerts, activity log
- AKS: Kubernetes clusters, node pools
- Networking: VNets, subnets, NSGs, load balancers
- Storage: Storage accounts
- Web: App Service, Function Apps
"""

from meho_app.modules.connectors.azure.connector import AzureConnector
from meho_app.modules.connectors.azure.operations import AZURE_OPERATIONS, AZURE_OPERATIONS_VERSION
from meho_app.modules.connectors.azure.types import AZURE_TYPES

__all__ = [
    "AZURE_OPERATIONS",
    "AZURE_OPERATIONS_VERSION",
    "AZURE_TYPES",
    "AzureConnector",
]

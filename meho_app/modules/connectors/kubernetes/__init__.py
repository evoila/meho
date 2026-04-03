# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Connector (TASK-159).

Implements the BaseConnector interface using kubernetes-asyncio for
native Kubernetes API access with pre-defined operations.
"""

from meho_app.modules.connectors.kubernetes.connector import KubernetesConnector
from meho_app.modules.connectors.kubernetes.operations import (
    KUBERNETES_OPERATIONS,
    KUBERNETES_OPERATIONS_VERSION,
)
from meho_app.modules.connectors.kubernetes.sync import (
    sync_all_kubernetes_connectors,
    sync_kubernetes_operations_if_needed,
)
from meho_app.modules.connectors.kubernetes.types import KUBERNETES_TYPES

__all__ = [
    "KUBERNETES_OPERATIONS",
    "KUBERNETES_OPERATIONS_VERSION",
    "KUBERNETES_TYPES",
    "KubernetesConnector",
    "sync_all_kubernetes_connectors",
    "sync_kubernetes_operations_if_needed",
]

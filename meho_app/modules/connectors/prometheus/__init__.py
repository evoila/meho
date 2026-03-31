# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Connector.

Typed connector for Prometheus HTTP API with pre-defined operations
for infrastructure metrics, service RED metrics, discovery, and alerts.
"""

from meho_app.modules.connectors.prometheus.connector import PrometheusConnector
from meho_app.modules.connectors.prometheus.operations import (
    PROMETHEUS_OPERATIONS,
    PROMETHEUS_OPERATIONS_VERSION,
)
from meho_app.modules.connectors.prometheus.types import PROMETHEUS_TYPES

__all__ = [
    "PROMETHEUS_OPERATIONS",
    "PROMETHEUS_OPERATIONS_VERSION",
    "PROMETHEUS_TYPES",
    "PrometheusConnector",
]

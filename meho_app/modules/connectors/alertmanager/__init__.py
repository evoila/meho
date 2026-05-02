# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Typed connector for Alertmanager HTTP API with pre-defined operations
for alert investigation, silence management, and cluster status.
No topology entities -- alerts are ephemeral.

9 operations across 3 categories:
- Alerts: list_alerts, get_firing_alerts, get_alert_detail
- Silences: list_silences, create_silence, silence_alert, expire_silence
- Status: get_cluster_status, list_receivers
"""

from meho_app.modules.connectors.alertmanager.connector import AlertmanagerConnector
from meho_app.modules.connectors.alertmanager.handlers import (
    AlertHandlerMixin,
    SilenceHandlerMixin,
    StatusHandlerMixin,
)
from meho_app.modules.connectors.alertmanager.operations import (
    ALERT_OPERATIONS,
    ALERTMANAGER_OPERATIONS,
    ALERTMANAGER_OPERATIONS_VERSION,
    SILENCE_OPERATIONS,
    STATUS_OPERATIONS,
    WRITE_OPERATIONS,
)

__all__ = [
    "ALERTMANAGER_OPERATIONS",
    "ALERTMANAGER_OPERATIONS_VERSION",
    "ALERT_OPERATIONS",
    "SILENCE_OPERATIONS",
    "STATUS_OPERATIONS",
    "WRITE_OPERATIONS",
    "AlertHandlerMixin",
    "AlertmanagerConnector",
    "SilenceHandlerMixin",
    "StatusHandlerMixin",
]

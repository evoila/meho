# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Handler Mixins (Phase 92).

Handler mixins for different Azure service categories.
"""

from meho_app.modules.connectors.azure.handlers.aks_handlers import AKSHandlerMixin
from meho_app.modules.connectors.azure.handlers.compute_handlers import ComputeHandlerMixin
from meho_app.modules.connectors.azure.handlers.monitor_handlers import MonitorHandlerMixin
from meho_app.modules.connectors.azure.handlers.network_handlers import NetworkHandlerMixin
from meho_app.modules.connectors.azure.handlers.storage_handlers import StorageHandlerMixin
from meho_app.modules.connectors.azure.handlers.web_handlers import WebHandlerMixin

__all__ = [
    "AKSHandlerMixin",
    "ComputeHandlerMixin",
    "MonitorHandlerMixin",
    "NetworkHandlerMixin",
    "StorageHandlerMixin",
    "WebHandlerMixin",
]

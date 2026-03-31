# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Connector Handler Mixins.

Each mixin provides operation handlers for a category of ArgoCD operations.
"""

from .application_handlers import ApplicationHandlerMixin
from .diff_handlers import DiffHandlerMixin
from .history_handlers import HistoryHandlerMixin
from .resource_handlers import ResourceHandlerMixin
from .sync_handlers import SyncHandlerMixin

__all__ = [
    "ApplicationHandlerMixin",
    "DiffHandlerMixin",
    "HistoryHandlerMixin",
    "ResourceHandlerMixin",
    "SyncHandlerMixin",
]

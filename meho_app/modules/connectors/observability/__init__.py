# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Shared Observability Connector Infrastructure.

Provides base class, time range model, and data reduction utilities
reused by Prometheus, Loki, Tempo, and Alertmanager connectors.
"""

from meho_app.modules.connectors.observability.base import ObservabilityHTTPConnector
from meho_app.modules.connectors.observability.data_reduction import (
    summarize_instant,
    summarize_time_series,
)
from meho_app.modules.connectors.observability.time_range import TimeRange

__all__ = [
    "ObservabilityHTTPConnector",
    "TimeRange",
    "summarize_instant",
    "summarize_time_series",
]

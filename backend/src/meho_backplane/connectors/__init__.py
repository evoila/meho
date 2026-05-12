# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors — connector contract public API.

Re-exports the ABC, all result models, and the registry functions so
downstream consumers can import from this package root.
"""

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    all_connectors,
    get_connector,
    register_connector,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = [
    "AuthModel",
    "Connector",
    "FingerprintResult",
    "OperationResult",
    "ProbeResult",
    "all_connectors",
    "get_connector",
    "register_connector",
]

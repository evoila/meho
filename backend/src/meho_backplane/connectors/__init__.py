# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors — connector contract public API.

Re-exports the ABC and all result models so downstream consumers can import
from this package root rather than from the submodules.
"""

from meho_backplane.connectors.base import Connector
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
]

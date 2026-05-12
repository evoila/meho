# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Abstract base class for all MEHO connectors.

Every connector implementation (VaultConnector, HttpConnector, etc.) inherits
from :class:`Connector` and provides the three async methods that constitute
the v0.2 surface. The ``Target`` placeholder is replaced with a concrete import
in T5 once G0.3 lands the Target model.
"""

from abc import ABC, abstractmethod
from typing import Any

from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult

__all__ = ["Connector"]

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# in G0.2-T5 once G0.3 lands the Target model.
type Target = Any


class Connector(ABC):
    """Abstract base for all MEHO connectors.

    Subclasses register operations via the per-product op_map (see T5
    reference impl). Three required methods cover the v0.2 surface;
    v0.2.next may add streaming.
    """

    product: str  # set on subclass: "vsphere", "vault", "bind9", etc.

    @abstractmethod
    async def fingerprint(self, target: Target) -> FingerprintResult:
        """Return the canonical fingerprint shape."""

    @abstractmethod
    async def probe(self, target: Target) -> ProbeResult:
        """Lightweight reachability + auth-challenge check."""

    @abstractmethod
    async def execute(
        self,
        target: Target,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Run a typed operation. op_id namespace: <product>.<resource>.<verb>."""

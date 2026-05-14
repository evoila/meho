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

    Subclasses advertise themselves through five class-level attributes that
    the G0.6 registry v2 (#393) keys on:

    * :attr:`product` — product slug, e.g. ``"vsphere"``, ``"vault"``,
      ``"bind9"``.
    * :attr:`version` — connector implementation version
      (e.g. ``"9.0"`` for a vSphere 9.0 connector). Empty string means
      "unversioned" and preserves v1 single-product registry behaviour.
    * :attr:`impl_id` — implementation discriminator, e.g.
      ``"vmware-rest"`` vs ``"vmware-pyvmomi"``. Empty string preserves
      v1 behaviour.
    * :attr:`supported_version_range` — PEP 440-style version spec
      (e.g. ``">=8.5,<10.0"``) the connector advertises against a
      target's fingerprinted product version. ``None`` means "any
      version" and preserves v1 behaviour.
    * :attr:`priority` — integer tie-break for the registry v2 resolver
      (#393) when two connectors match the same ``(product, version)``;
      higher wins.

    The defaults on the four new attributes are chosen so existing v1
    subclasses (VaultConnector — #244; KubernetesConnector skeleton —
    #321) keep working without modification. Three required async methods
    cover the v0.2 surface; v0.2.next may add streaming.
    """

    # Set on subclass: "vsphere", "vault", "bind9", etc.
    product: str

    # G0.6-T3 (#394) — registry v2 metadata. Defaults preserve v1 behaviour.
    version: str = ""
    impl_id: str = ""
    supported_version_range: str | None = None
    priority: int = 0

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
        """Run a typed operation.

        op_id namespace varies by source_kind:

        * ``'ingested'``  — ``"{METHOD}:{path}"``
          (e.g. ``"GET:/api/vcenter/cluster"``).
        * ``'typed'``     — dotted shape per product
          (e.g. ``"vault.kv.read"``).
        * ``'composite'`` — dotted with ``.composite`` suffix
          (e.g. ``"vmware.composite.vm.create"``).

        In v0.2.next post-G0.6, this method is typically called BY the
        G0.6 dispatcher AFTER lookup + validation; subclasses don't
        implement their own dispatch tables (``register_typed_operation()``
        handles registration). Subclasses MAY still override ``execute()``
        for special transport semantics (streaming, batching) but the
        common path is "look up handler_ref from endpoint_descriptor,
        call the handler".
        """

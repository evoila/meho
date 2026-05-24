# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`HolodeckConnector`.

G3.8-T1 (#853) skeleton ships ``holodeck.about`` -- the canary op
that proves the ``register_typed_operation()`` -> dispatcher ->
PowerShell-over-SSH -> JSON-parse pipeline end-to-end on the
Holodeck connector. G3.8-T2 (#854) appends the 8 read ops via
``_holodeck_ops()`` exactly as the bind9 / pfSense siblings layered
their read groups onto the T1 canary.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.bind9.ops` and
:mod:`~meho_backplane.connectors.pfsense.ops` so the registration
walk reads identically across SSH-transport connectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["HOLODECK_OPS", "HolodeckOp"]


@dataclass(frozen=True)
class HolodeckOp:
    """Metadata for one Holodeck op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.holodeck.connector.HolodeckConnector`
    that exposes the async handler.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The single T1 canary op. ``holodeck.about`` is the operator-facing
#: wrapper around :meth:`HolodeckConnector.fingerprint`. T2 (#854)
#: appends the 8 read ops onto :data:`HOLODECK_OPS` from
#: ``ops_read``-style modules.
_HOLODECK_ABOUT_OP = HolodeckOp(
    op_id="holodeck.about",
    handler_attr="about",
    summary="Return the Holodeck appliance's product, version, and Photon OS snapshot.",
    description=(
        "Connects to the HoloRouter appliance over SSH and runs "
        "``cat /etc/photon-release`` plus ``pwsh -EncodedCommand`` "
        "of ``Get-HoloDeckConfig | ConvertTo-Json -Compress`` to "
        "extract the Holodeck version, Photon OS version, and pod "
        "ID. Returns a flat dict with the parsed Holodeck version, "
        "the full Photon release line, the pod ID, and the vendor "
        "(``vmware``). Holodeck exposes no REST API; this op (and "
        "every other op in the connector) is the canonical surface "
        "for identifying the appliance before issuing higher-level "
        "pod/service/log ops. No params; safe to call on any healthy "
        "HoloRouter target."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "build": {"type": ["string", "null"]},
            "photon_version": {"type": ["string", "null"]},
            "pod_id": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "holodeck"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the Holodeck "
            "appliance behind a target before issuing higher-level "
            "pod / service / log ops, or when the agent needs to "
            "confirm the appliance is reachable via SSH + pwsh. "
            "Holodeck has no REST surface; this op and the other "
            "Holodeck ops are reached through PowerShell-over-SSH."
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the parsed Holodeck "
            "version string (e.g. ``9.0.0``) when the cmdlet output "
            "was readable. ``photon_version`` carries the host "
            "Photon OS release identifier (e.g. ``5.0``). ``pod_id`` "
            "carries the Holodeck pod identifier when set on the "
            "appliance, ``None`` otherwise."
        ),
    },
)


#: The ops :class:`HolodeckConnector` registers at lifespan startup.
#:
#: T1 ships ``holodeck.about``; T2 (#854) appends the 8 read ops
#: (``holodeck.config.show``, ``holodeck.pod.list``,
#: ``holodeck.pod.info``, ``holodeck.service.list``,
#: ``holodeck.k8s.exec``, ``holodeck.logs.tail``,
#: ``holodeck.networking.show``) onto this tuple via an ``ops_read``
#: composition module -- the registration walk in
#: :meth:`HolodeckConnector.register_operations` does not change.
HOLODECK_OPS: tuple[HolodeckOp, ...] = (_HOLODECK_ABOUT_OP,)

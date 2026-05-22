# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`PfSenseConnector`.

G3.7-T1 (#844) skeleton ships ``pfsense.about`` -- the canary op that
proves the ``register_typed_operation()`` -> dispatcher -> handler ->
result-wrap pipeline end-to-end for the pfSense SSH-transport connector.
G3.7-T2 (#847) layers the 7 read ops (``pfctl``/config.xml parsed)
onto that surface.

The dataclass + tuple shape mirrors the Bind9 connector
(:mod:`~meho_backplane.connectors.bind9.ops`) so the registration
walk reads identically to the bind9 sibling. The ``PfSenseOp``
dataclass mirrors ``Bind9Op`` field for field, preserving the
uniform registration-walk contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["PFSENSE_OPS", "PfSenseOp"]


@dataclass(frozen=True)
class PfSenseOp:
    """Metadata for one pfSense op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.pfsense.connector.PfSenseConnector`
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


#: The single T1 canary op. ``pfsense.about`` is the operator-facing
#: wrapper around :meth:`PfSenseConnector.fingerprint` -- the standard
#: product/version/build payload surfaced through the typed-op dispatcher.
#: T2 (#847) appends the 7 read ops.
_PFSENSE_ABOUT_OP = PfSenseOp(
    op_id="pfsense.about",
    handler_attr="about",
    summary="Return the pfSense firewall's product, version, and build information.",
    description=(
        "Connects to the target pfSense firewall over SSH and reads "
        "``/etc/version`` to extract the pfSense version and FreeBSD "
        "build string. Returns a flat dict with the parsed version "
        "(e.g. ``2.7.2``), the full build line, the kernel identifier, "
        "and the vendor (``netgate``). Use to confirm the firewall is "
        "reachable and identify its version before issuing higher-level "
        "pfSense ops. No params; safe to call on any healthy pfSense "
        "target with SSH key auth configured."
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
            "kernel": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "pfsense"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the pfSense "
            "version behind a target before issuing higher-level "
            "firewall or NAT ops, or when the agent needs to confirm "
            "the firewall is reachable over SSH with shell access."
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the parsed pfSense "
            "release string (e.g. ``2.7.2``) if the ``/etc/version`` "
            "file was readable. ``build`` carries the full build line "
            "(including FreeBSD version and architecture). ``kernel`` "
            "carries the FreeBSD kernel identifier."
        ),
    },
)


#: The ops :class:`PfSenseConnector` registers at lifespan startup.
#: T1 ships the single ``pfsense.about`` canary; T2 (#847) will extend
#: via a ``_pfsense_ops()`` composition function mirroring the bind9
#: ``_bind9_ops()`` pattern.
PFSENSE_OPS: tuple[PfSenseOp, ...] = (_PFSENSE_ABOUT_OP,)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`PfSenseConnector`.

G3.7-T1 (#844) skeleton ships ``pfsense.about`` -- the canary op that
proves the ``register_typed_operation()`` -> dispatcher -> handler ->
result-wrap pipeline end-to-end for the pfSense SSH-transport connector.
G3.7-T2 (#847) layers the 7 read ops (``pfctl``/config.xml parsed)
onto that surface via :func:`_pfsense_ops`.

The dataclass + tuple shape mirrors the Bind9 connector
(:mod:`~meho_backplane.connectors.bind9.ops`) so the registration
walk reads identically to the bind9 sibling. The ``PfSenseOp``
dataclass mirrors ``Bind9Op`` field for field, preserving the
uniform registration-walk contract.

The composition pattern (:func:`_pfsense_ops` importing per-module op
tuples) mirrors :func:`meho_backplane.connectors.bind9.ops._bind9_ops`
-- ``ops.py`` defines :class:`PfSenseOp` + ``_PFSENSE_ABOUT_OP``,
then imports the T2 read ops from :mod:`ops_read`. Import order stays
linear; the ``about`` canary stays co-located with the dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["PFSENSE_OPS", "PfSenseOp", "_pfsense_ops"]


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


def _pfsense_ops() -> tuple[PfSenseOp, ...]:
    """Return the merged registration tuple.

    Composition: ``pfsense.about`` (T1 canary) + ``READ_OPS`` (T2 read
    ops: ``pfsense.version``, ``pfsense.firewall.rules``,
    ``pfsense.firewall.state``, ``pfsense.nat.rules``,
    ``pfsense.interface.list``, ``pfsense.gateway.list``,
    ``pfsense.config.show``). Eight ops total -- the full G3.7-T2
    read surface.

    Implemented as a function call rather than a literal-and-splat at
    module level so the import order stays linear: ``ops.py`` defines
    :class:`PfSenseOp` + ``_PFSENSE_ABOUT_OP``, then imports the T2
    read ops from :mod:`meho_backplane.connectors.pfsense.ops_read`.
    The arrangement keeps the canary op co-located with the dataclass
    while the larger read surface lives in its own module next to its
    parsers. Mirrors :func:`meho_backplane.connectors.bind9.ops._bind9_ops`.
    """
    from meho_backplane.connectors.pfsense.ops_read import READ_OPS

    return (_PFSENSE_ABOUT_OP, *READ_OPS)


#: The ops :class:`PfSenseConnector` registers at lifespan startup.
#: T1 shipped ``pfsense.about``; T2 (#847) adds the 7 read ops
#: (``pfsense.version``, ``pfsense.firewall.rules``,
#: ``pfsense.firewall.state``, ``pfsense.nat.rules``,
#: ``pfsense.interface.list``, ``pfsense.gateway.list``,
#: ``pfsense.config.show``) -- 8 ops total. The shape of each
#: follow-on PR is "import a new module-level tuple and splat it
#: into :data:`PFSENSE_OPS` via :func:`_pfsense_ops`" -- the
#: registration walk in
#: :meth:`PfSenseConnector.register_operations` does not need to
#: change.
PFSENSE_OPS: tuple[PfSenseOp, ...] = _pfsense_ops()

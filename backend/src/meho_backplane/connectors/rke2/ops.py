# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`Rke2SshConnector`.

G-Node/RKE2-T1 (#2221) scaffold -- ships the read-only tier only:

* ``rke2.about`` -- the identity canary. Operator-facing wrapper around
  :meth:`Rke2SshConnector.fingerprint` (``rke2 --version`` +
  ``/etc/os-release``). Proves the ``register_typed_operation()`` ->
  dispatcher -> plain-SSH -> parse pipeline end-to-end, exactly as the
  bind9 / holodeck siblings' ``about`` canaries did.
* ``rke2.posture.show`` -- the read-only posture tier. ``stat``s the
  RKE2 config-file modes and the on-disk join-token presence with the
  token **value never read** (redacted by construction). Two ops total.

The approval-gated write ops land in
:mod:`meho_backplane.connectors.rke2.ops_write` and are composed onto
:data:`RKE2_OPS` here via :data:`WRITE_OPS`: ``rke2.token.rotate`` (T2 #2429)
plus ``rke2.node.service.restart`` / ``rke2.node.config.update`` (T3 #2430).
The safe, non-gated ``rke2.etcd-snapshot.save`` op (T4 #2431) is composed
in from :mod:`~meho_backplane.connectors.rke2.ops_snapshot` via
:data:`SNAPSHOT_OPS` -- it is the lone non-gated op in the Initiative #2172
surface.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.bind9.ops` and
:mod:`~meho_backplane.connectors.holodeck.ops` so the registration walk
reads identically across SSH-transport connectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["RKE2_OPS", "SSH_TRANSPORT_NOTE", "Rke2Op", "_rke2_ops"]


#: Canonical plain-SSH transport reminder copied verbatim into every op's
#: ``llm_instructions.when_to_use``. RKE2 nodes expose no MEHO REST
#: surface; every op is plain SSH to the node OS. The read tier never
#: reads secret material -- it ``stat``s modes + presence only.
SSH_TRANSPORT_NOTE: str = (
    "RKE2 cluster nodes expose no MEHO REST API; the transport is plain "
    "SSH to the node OS over the shared SSH adapter. The read-only "
    "posture tier only ``stat``s file modes and token-file presence -- "
    "it never reads secret material (the join token value is redacted by "
    "construction)."
)


@dataclass(frozen=True)
class Rke2Op:
    """Metadata for one RKE2 op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can
    splat the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.rke2.connector.Rke2SshConnector`
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


#: The identity canary. ``rke2.about`` wraps :meth:`fingerprint`.
_RKE2_ABOUT_OP = Rke2Op(
    op_id="rke2.about",
    handler_attr="about",
    summary="Return the RKE2 node's vendor, product, RKE2 version, and node OS.",
    description=(
        "Connects to a cluster node over SSH and runs ``rke2 --version`` "
        "plus ``cat /etc/os-release`` to identify the node. Returns a "
        "flat dict with the vendor (``rancher``), product (``rke2``), the "
        "parsed RKE2 release string (e.g. ``v1.28.5+rke2r1``, or ``null`` "
        "when the ``rke2`` binary is not on PATH), and the node OS "
        "pretty-name. No params; safe to call on any reachable node. "
        "Call this first to confirm the node is reachable via SSH before "
        "issuing the posture / (future) maintenance ops."
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
            "node_os": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "rke2"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the RKE2 node "
            "behind a target -- which RKE2 version it runs and on which "
            "node OS -- or to confirm the node is reachable via SSH "
            "before issuing the posture op. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the parsed RKE2 release "
            "string (e.g. ``v1.28.5+rke2r1``) when the ``rke2`` binary "
            "was on PATH, ``null`` otherwise. ``node_os`` carries the "
            "``/etc/os-release`` PRETTY_NAME (e.g. ``Ubuntu 22.04 LTS``)."
        ),
    },
)


def _rke2_ops() -> tuple[Rke2Op, ...]:
    """Return the merged registration tuple.

    Composition: ``rke2.about`` (identity canary) + ``READ_OPS`` (the
    read-only posture tier: ``rke2.posture.show``) + ``WRITE_OPS`` (the
    approval-gated write tier: ``rke2.token.rotate`` (T2 #2429) plus
    ``rke2.node.service.restart`` / ``rke2.node.config.update`` (T3 #2430))
    + ``SNAPSHOT_OPS`` (the safe, non-gated ``rke2.etcd-snapshot.save`` --
    T4 #2431). This layers the write + snapshot tiers onto the read surface
    exactly as
    :func:`meho_backplane.connectors.holodeck.ops._holodeck_ops` layers its
    ``WRITE_OPS`` on.

    Implemented as a function call rather than a module-level literal so
    the import order stays linear: ``ops.py`` defines :class:`Rke2Op` +
    ``_RKE2_ABOUT_OP``, then imports the per-tier ops from their sibling
    modules.
    """
    from meho_backplane.connectors.rke2.ops_read import READ_OPS
    from meho_backplane.connectors.rke2.ops_snapshot import SNAPSHOT_OPS
    from meho_backplane.connectors.rke2.ops_write import WRITE_OPS

    return (_RKE2_ABOUT_OP, *READ_OPS, *WRITE_OPS, *SNAPSHOT_OPS)


#: The ops :class:`Rke2SshConnector` registers at lifespan startup.
RKE2_OPS: tuple[Rke2Op, ...] = _rke2_ops()

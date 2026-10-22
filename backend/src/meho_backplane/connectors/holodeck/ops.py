# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`HolodeckConnector`.

G3.8-T1 (#853) skeleton shipped ``holodeck.about`` -- the canary op
that proves the ``register_typed_operation()`` -> dispatcher ->
PowerShell-over-SSH -> JSON-parse pipeline end-to-end on the
Holodeck connector. G3.8-T2 (#854) appends 7 read ops
(``holodeck.config.show`` / ``holodeck.pod.list`` /
``holodeck.pod.info`` / ``holodeck.service.list`` /
``holodeck.k8s.exec`` / ``holodeck.logs.tail`` /
``holodeck.networking.show``) via :func:`_holodeck_ops`, exactly as
the bind9 / pfSense siblings layered their read groups onto the
T1 canary -- 8 ops total under ``connector_id="holodeck-ssh-9.0"``.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.bind9.ops` and
:mod:`~meho_backplane.connectors.pfsense.ops` so the registration
walk reads identically across SSH-transport connectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from meho_backplane.connectors.holodeck._response_schemas import HOLODECK_RESPONSE_SCHEMAS

__all__ = ["HOLODECK_OPS", "SSH_TRANSPORT_NOTE", "HolodeckOp", "_holodeck_ops"]


#: Canonical SSH-only/pwsh transport reminder copied verbatim into every
#: op's ``llm_instructions``. The Initiative #371 body and CLAUDE.md
#: postulate 5 both require agent-facing descriptions to call out the
#: PowerShell-over-SSH transport so an LLM doesn't compose against a
#: non-existent REST surface. G3.8-T3 (#855) hoisted this constant from
#: ``ops_read.py`` into ``ops.py`` so the T1 canary op (``holodeck.about``,
#: defined here) can share it verbatim with the 7 T2 read ops -- the MCP
#: review acceptance criterion on #855 calls for the same wording across
#: all 8 ops, and one shared constant is the cleanest way to keep that
#: true forever (a future op author can't accidentally drift).
SSH_TRANSPORT_NOTE: str = (
    "Holodeck has no REST API; the underlying transport is "
    "PowerShell-over-SSH (pwsh -EncodedCommand routed through asyncssh) "
    "for cmdlet ops, plain SSH for kubectl / shell-pipeline ops."
)


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
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The single T1 canary op. ``holodeck.about`` is the operator-facing
#: wrapper around :meth:`HolodeckConnector.fingerprint`. T2 (#854)
#: appends the 8 read ops onto :data:`HOLODECK_OPS` from
#: ``ops_read``-style modules.
_HOLODECK_ABOUT_OP = HolodeckOp(
    op_id="holodeck.about",
    handler_attr="about",
    summary="Show the Holodeck version, Photon OS version and pod id of the HoloRouter.",
    description=(
        "Connects to the HoloRouter over SSH, reads /etc/photon-release and runs "
        "Get-HoloDeckConfig. Returns vendor, product, Holodeck version, Photon "
        "OS version and pod id. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=HOLODECK_RESPONSE_SCHEMAS["holodeck.about"],
    group_key="identity",
    tags=("read-only", "identity", "holodeck"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first, to learn which Holodeck version a HoloRouter runs and "
            "to check that MEHO can reach it over SSH and PowerShell. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "{vendor, product, version, build, photon_version, pod_id}. "
            "'version' is the Holodeck version, for example 9.0.0, or null when "
            "the cmdlet answer had none. 'build' is the first line of "
            "/etc/photon-release. 'pod_id' is null when the appliance has none."
        ),
        "next_step": (
            "For the full configuration, call holodeck.config.show. For the "
            "services, call holodeck.service.list."
        ),
    },
)


def _holodeck_ops() -> tuple[HolodeckOp, ...]:
    """Return the merged registration tuple.

    Composition: ``holodeck.about`` (T1 canary) + ``READ_OPS`` (T2
    read ops: ``holodeck.config.show``, ``holodeck.pod.list``,
    ``holodeck.pod.info``, ``holodeck.service.list``,
    ``holodeck.k8s.exec``, ``holodeck.logs.tail``,
    ``holodeck.networking.show``). Eight ops total -- the full G3.8-T2
    read surface.

    Implemented as a function call rather than a literal-and-splat at
    module level so the import order stays linear: ``ops.py`` defines
    :class:`HolodeckOp` + ``_HOLODECK_ABOUT_OP``, then imports the T2
    read ops from :mod:`meho_backplane.connectors.holodeck.ops_read`.
    Mirrors :func:`meho_backplane.connectors.pfsense.ops._pfsense_ops`
    and :func:`meho_backplane.connectors.bind9.ops._bind9_ops`.

    G3.18-T1 (#2153) appends ``holodeck.disk.usage`` and #2847 appends
    ``holodeck.backups.list`` (the safe read half of the ``backups``
    surface). G3.18-T2 (#2154) appends the 3 approval-gated remediation
    write ops (:data:`~meho_backplane.connectors.holodeck.ops_write.WRITE_OPS`:
    ``holodeck.k8s.pods.gc`` / ``holodeck.backups.prune`` /
    ``holodeck.images.import``) onto the tuple -- 13 ops. #2908 appends the
    4 deploy-lifecycle ops
    (:data:`~meho_backplane.connectors.holodeck.ops_deploy.DEPLOY_OPS`:
    ``holodeck.config.apply`` / ``holodeck.instance.start`` /
    ``holodeck.instance.status`` / ``holodeck.router.patch``) -- 17 ops total.
    """
    from meho_backplane.connectors.holodeck.ops_deploy import DEPLOY_OPS
    from meho_backplane.connectors.holodeck.ops_read import READ_OPS
    from meho_backplane.connectors.holodeck.ops_write import WRITE_OPS

    return (_HOLODECK_ABOUT_OP, *READ_OPS, *WRITE_OPS, *DEPLOY_OPS)


#: The ops :class:`HolodeckConnector` registers at lifespan startup.
#:
#: T1 shipped ``holodeck.about``; T2 (#854) adds the 7 read ops
#: (``holodeck.config.show``, ``holodeck.pod.list``,
#: ``holodeck.pod.info``, ``holodeck.service.list``,
#: ``holodeck.k8s.exec``, ``holodeck.logs.tail``,
#: ``holodeck.networking.show``); G3.18-T1 (#2153) appends
#: ``holodeck.disk.usage`` and #2847 appends ``holodeck.backups.list``
#: -- 10 read ops (13 total with the 3 G3.18-T2 write ops). The
#: registration walk in :meth:`HolodeckConnector.register_operations`
#: does not need to change as ops are added; only :func:`_holodeck_ops`
#: composes the merged tuple.
HOLODECK_OPS: tuple[HolodeckOp, ...] = _holodeck_ops()

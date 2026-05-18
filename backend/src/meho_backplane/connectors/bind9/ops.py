# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`Bind9Connector`.

G3.4-T1 (#587) skeleton ships ``bind9.about`` -- the canary op that
proves the ``register_typed_operation()`` -> dispatcher -> handler ->
result-wrap pipeline end-to-end for an SSH-transport connector. The
remaining 10 ops (zone / record / config reads + writes) land under
G3.4-T2..T4 (#588 / #589 / #590) by extending :data:`BIND9_OPS` from
their own modules; this file holds only the T1 canary so the
skeleton's surface area stays narrow.

The dataclass + tuple shape mirrors the Kubernetes connector
(:mod:`~meho_backplane.connectors.kubernetes.ops`) so the registration
walk in :meth:`Bind9Connector.register_operations` reads identically
to the k8s sibling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["BIND9_OPS", "Bind9Op"]


@dataclass(frozen=True)
class Bind9Op:
    """Metadata for one bind9 op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.bind9.connector.Bind9Connector`
    that exposes the async handler; the connector resolves the bound
    method against itself at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    walk can recover the callable from the persisted
    ``module.ClassName.method`` dotted path.
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


#: The single T1 canary op. ``bind9.about`` is the operator-facing
#: wrapper around :meth:`Bind9Connector.fingerprint` -- same product /
#: version / build payload, surfaced through the typed-op dispatcher
#: so callers see the standard :class:`OperationResult` envelope
#: instead of the raw :class:`FingerprintResult`. Mirrors the
#: :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`
#: ``k8s.about`` entry; T2..T4 append the remaining bind9 ops onto
#: the merged tuple from their own modules.
_BIND9_ABOUT_OP = Bind9Op(
    op_id="bind9.about",
    handler_attr="about",
    summary="Return the bind9 nameserver's product, version, and host OS.",
    description=(
        "Hits the target nameserver over SSH and runs ``named -v`` to "
        "read the BIND version banner (e.g. "
        "``BIND 9.18.24-1+deb12u2-Debian``) plus ``/etc/os-release`` "
        "(or ``/etc/debian_version`` as a fallback) to identify the "
        "host OS. Returns a flat dict with the parsed version (e.g. "
        "``9.18.24``), the full banner string, the vendor (``isc``), "
        "and the OS identifier. Use to confirm the nameserver is "
        "reachable and identify its version before issuing higher-"
        "level ops; no params; safe to call on any healthy bind9 "
        "target."
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
            "os": {"type": ["string", "null"]},
            "named_conf_path": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "bind9"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the bind9 "
            "nameserver behind a target before issuing higher-level "
            "DNS ops (zone reads, record queries, etc.), or when the "
            "agent needs to pick a version-flavoured doc page from "
            "the knowledge base."
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; the ``version`` field carries the parsed "
            "BIND <X.Y.Z> triple if the banner was readable, "
            "``None`` otherwise. ``build`` carries the full banner "
            "string. ``os`` carries the host OS identifier from "
            "``/etc/os-release`` (e.g. ``debian 12``) or ``None``."
        ),
    },
)


#: The ops :class:`Bind9Connector` registers at lifespan startup.
#:
#: T1 ships only ``bind9.about``; T2 (#588) appends the read op group
#: (``bind9.zone.list/read``, ``bind9.record.get``,
#: ``bind9.config.show``); T3 (#589) appends the record-write group
#: (``bind9.record.add/remove``); T4 (#590) appends the config-write
#: group (``bind9.config.apply_views``, ``bind9.config.apply_file``,
#: ``bind9.config.backup``, ``bind9.config.reload``). The shape of
#: each follow-on PR is "import a new module-level tuple and splat it
#: into :data:`BIND9_OPS`" -- the registration walk in
#: :meth:`Bind9Connector.register_operations` does not need to change.
BIND9_OPS: tuple[Bind9Op, ...] = (_BIND9_ABOUT_OP,)

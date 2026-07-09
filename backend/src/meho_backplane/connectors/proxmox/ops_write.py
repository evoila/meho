# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated write passthrough for :class:`ProxmoxConnector` (#2238).

Proxmox VE is the one **read/write** connector in Initiative #2228. It has no
code-level GET gate: write authorisation leans entirely on MEHO's policy gate
+ approval queue. This module ships the single generic write op,
``proxmox.api.write`` (POST/PUT/DELETE passthrough), registered
``safety_level="dangerous"`` + ``requires_approval=True`` — the G3.x
write-surface mold (``connectors/argocd/ops_write_schemas.py``).

Approval routing (load-bearing)
===============================

``requires_approval=True`` is the only knob the connector controls, and it is
what engages the queue. The dispatcher's policy gate
(:func:`~meho_backplane.operations._validate.policy_gate`) routes a
``USER``-principal dispatch of a ``requires_approval`` op to the human
approve-queue (``needs-approval`` / ``awaiting_approval``) rather than
hard-denying it (G11.7-T1 #1401), and floors an agent dispatch to
needs-approval regardless of safety level. :func:`proxmox_api_write`
therefore runs only on the ``_approved=True`` resume path, after a human has
approved the parked request. There is no bypass and no hard-deny.

Async writes → UPID
===================

Most Proxmox state changes (VM clone/start/stop/shutdown/delete, migrations,
backups) run as **background tasks**: the write returns immediately with a
``UPID`` string in ``data`` rather than the finished result. The op returns
the raw ``data`` (the UPID) plus a parsed ``{upid, node}`` when the payload is
a UPID string, so the agent can follow it with ``proxmox.task.status``
(``wait=true``) to completion. Body params are sent form-encoded
(``application/x-www-form-urlencoded``) — the canonical Proxmox write shape;
query params travel in the URL query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.proxmox.ops import (
    API_PATH_PATTERN,
    WRITE_METHODS,
    ProxmoxOp,
    join_api_path,
    validate_api_path,
    validate_method,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.proxmox.connector import ProxmoxConnector
    from meho_backplane.connectors.proxmox.session import ProxmoxTargetLike

__all__ = [
    "PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP",
    "PROXMOX_WRITE_OPS",
    "parse_upid",
    "proxmox_api_write",
]


_WHEN_TO_USE_API_WRITE = (
    "Use to CHANGE Proxmox VE state via a single generic POST/PUT/DELETE op "
    "(proxmox.api.write): clone a VM (POST nodes/{node}/qemu/{vmid}/clone), "
    "start/stop/shutdown a VM (POST nodes/{node}/qemu/{vmid}/status/start|"
    "stop|shutdown), delete a VM (DELETE nodes/{node}/qemu/{vmid}), change "
    "config, and so on. EVERY call is approval-gated: the dispatch parks for "
    "a human to approve before it touches the cluster. Most writes return a "
    "UPID for a background task — follow it with proxmox.task.status. The "
    "right op when the operator wants to CHANGE state, not inspect it (use "
    "proxmox.api.get for reads)."
)

#: Curated write ``when_to_use`` blurb, keyed by the ``-write``-suffixed group
#: so it never collides with the read-op groups in
#: :data:`~meho_backplane.connectors.proxmox.ops.PROXMOX_WHEN_TO_USE_BY_GROUP`.
PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "proxmox-api-write": _WHEN_TO_USE_API_WRITE,
}


PROXMOX_WRITE_OPS: tuple[ProxmoxOp, ...] = (
    ProxmoxOp(
        op_id="proxmox.api.write",
        handler_attr="api_write",
        summary="Generic write (POST/PUT/DELETE) of any Proxmox REST path (approval-gated).",
        description=(
            "Issues a POST/PUT/DELETE against a relative Proxmox API path "
            "mounted under the fixed /api2/json base, with a form-encoded "
            "body. The path is gated by the same two-layer allowlist as the "
            "read passthrough (schema pattern + fail-closed handler); the "
            "method allowlist ({POST,PUT,DELETE}) enforces the write half of "
            "the read/write split. safety_level=dangerous, "
            "requires_approval=True — the dispatch parks in the approval queue "
            "and this handler runs only after a human approves. Returns the "
            "endpoint's 'data' (a UPID for a background task) plus a parsed "
            "{upid, node} when the result is a UPID."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": API_PATH_PATTERN,
                    "description": (
                        "Relative Proxmox API path under /api2/json (no leading "
                        "slash, no '..'), e.g. "
                        "'nodes/pve/qemu/100/status/start'."
                    ),
                },
                "method": {
                    "type": "string",
                    "enum": sorted(WRITE_METHODS),
                    "description": "Write verb: POST, PUT, or DELETE.",
                },
                "body": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "Form-encoded write parameters (flat scalars or lists), "
                        "e.g. {'newid': 9001, 'name': 'clone-1'} for a clone."
                    ),
                },
                "query": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Optional URL query parameters.",
                },
            },
            "required": ["path", "method"],
            "additionalProperties": False,
        },
        response_schema=None,
        group_key="proxmox-api-write",
        tags=("write", "proxmox", "passthrough"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_API_WRITE,
            "parameter_hints": {
                "path": "Relative to /api2/json, e.g. 'nodes/pve/qemu/100/status/stop'.",
                "method": "POST to create/act, PUT to update, DELETE to remove.",
                "body": "Form params for the endpoint (see the PVE API viewer).",
            },
            "output_shape": (
                "{data, upid, node}. data is the raw endpoint payload; when it "
                "is a UPID string, upid/node are parsed out for "
                "proxmox.task.status."
            ),
        },
    ),
)


def parse_upid(data: Any) -> dict[str, str | None]:
    """Return ``{upid, node}`` parsed from a Proxmox write payload.

    Proxmox background writes return a ``UPID`` string in ``data`` of the
    shape ``UPID:<node>:<pid>:<pstart>:<starttime>:<type>:<id>:<user>:``. When
    *data* is such a string, the node is its second colon-delimited field.
    When *data* is not a UPID string (a synchronous write returning an object
    or ``None``), both fields are ``None``.
    """
    if isinstance(data, str) and data.startswith("UPID:"):
        parts = data.split(":")
        node = parts[1] if len(parts) > 1 and parts[1] else None
        return {"upid": data, "node": node}
    return {"upid": None, "node": None}


async def proxmox_api_write(
    self: ProxmoxConnector,
    operator: Operator,
    target: ProxmoxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``proxmox.api.write`` — approval-gated POST/PUT/DELETE passthrough.

    Re-validates the path + method (handler-layer allowlist), prepends the
    constant ``/api2/json`` base, and issues the mutating request via
    :meth:`ProxmoxConnector._write_json` (non-retried; attaches the
    ``CSRFPreventionToken`` header when the target authenticates by ticket).
    Returns ``{data, upid, node}``.
    """
    rel_path = validate_api_path(str(params["path"]))
    method = validate_method(str(params["method"]), WRITE_METHODS)
    body = params.get("body")
    query = params.get("query")
    data = await self._write_json(
        target,
        method,
        join_api_path(rel_path),
        operator=operator,
        data=body if isinstance(body, dict) else None,
        params=query if isinstance(query, dict) else None,
    )
    payload = data.get("data") if isinstance(data, dict) else data
    return {"data": payload, **parse_upid(payload)}

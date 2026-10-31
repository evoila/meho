# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared steps of the six host security changes.

Each change finds the host and one config manager, reads the setting first
(``before``), answers ``unchanged`` and sends nothing when the host already
has it, sends only the calls that change something through the governed
VI-JSON seam (:func:`._write._write_vmomi_sub_op`), then reads the setting
back (``after``). The calls are synchronous (no vCenter task). A vim fault
(HTTP 500 with the fault object) becomes ``failed`` with the fault class and
text and an ``error`` issue, so the call is recorded as failed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_security import locate
from meho_backplane.connectors.vmware_rest.vcenter_deployment import hide_secrets

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "Reader",
    "Step",
    "envelope",
    "finish",
    "preview_current",
    "refused",
    "run_steps",
    "stop",
    "unchanged",
]

#: One write: (governance op id, concrete VI-JSON path, body, gate params).
Step = tuple[str, str, dict[str, Any], dict[str, Any]]
#: Reads the setting of the host: (connector, operator, target, manager moid).
Reader = Callable[["VmwareRestConnector", "Operator", Any, str], Awaitable[Any]]

_READ_ERRORS: Final = (httpx.HTTPError, RuntimeError)


def envelope(host_moid: str | None, requested: dict[str, Any], before: Any) -> dict[str, Any]:
    """The answer of a change before its status is known."""
    return {
        "host": host_moid,
        "requested": requested,
        "before": before,
        "after": None,
        "applied_steps": [],
        "fault": None,
        "guidance": None,
    }


def refused(refusal: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    """A refusal before anything was read or sent."""
    return {**envelope(refusal.get("host"), requested, None), **refusal}


def stop(base: dict[str, Any], status: str, guidance: str, **extra: Any) -> dict[str, Any]:
    """A refusal after the read: nothing was sent."""
    return {**base, "status": status, "guidance": f"{guidance} Nothing was sent.", **extra}


def unchanged(base: dict[str, Any]) -> dict[str, Any]:
    """The host already has the setting; nothing was sent."""
    return {
        **base,
        "status": "unchanged",
        "after": base["before"],
        "guidance": "The host already has this setting; nothing was sent.",
    }


async def run_steps(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    steps: list[Step],
    base: dict[str, Any],
    secrets: tuple[str, ...] = (),
) -> OperationResult | dict[str, Any] | None:
    """Send *steps* in order through the governed seam.

    Returns ``None`` when all ran (their method names are in
    ``base["applied_steps"]``), the gate's :class:`OperationResult` when a step
    waits or is denied, or a ``failed`` answer on a vim fault. A fault text
    that holds one of *secrets* is replaced.
    """
    for op_id, path, body, params in steps:
        method = path.rsplit("/", 1)[-1]
        try:
            gate, _ = await _write_vmomi_sub_op(
                connector,
                target,
                operator,
                op_id=op_id,
                vmomi_path=path,
                body=body,
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 500:
                raise
            detail = hide_secrets(_vsphere_fault_detail(exc), secrets) or "HTTP 500"
            done = ", ".join(base["applied_steps"]) or "none"
            return {
                **base,
                "status": "failed",
                "fault": detail,
                "guidance": f"vSphere refused {method}. Calls that ran before it: {done}.",
                "issues": [issue("connector", "error", f"{method} failed: {detail}")],
            }
        if gate is not None:
            return gate
        base["applied_steps"].append(method)
    return None


async def finish(
    base: dict[str, Any],
    read: Callable[[], Awaitable[Any]],
    matches: Callable[[Any], bool],
) -> dict[str, Any]:
    """Read the setting back and set ``applied`` or ``not_verified``."""
    try:
        after = await read()
    except _READ_ERRORS as exc:
        return {
            **base,
            "status": "not_verified",
            "guidance": "The calls ran, but MEHO could not read the host again "
            f"({type(exc).__name__}). Read the setting again to check it.",
            "issues": [issue("connector", "error", "the change could not be read back")],
        }
    if not matches(after):
        return {
            **base,
            "status": "not_verified",
            "after": after,
            "guidance": "The calls ran, but the host does not show the new setting.",
            "issues": [issue("connector", "error", "the host does not show the new setting")],
        }
    return {**base, "status": "applied", "after": after}


async def preview_current(
    ctx: PreviewContext, manager: str, read: Reader, requested: dict[str, Any]
) -> dict[str, Any] | None:
    """The approval card: the host, its current setting and what is asked."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        host_moid, manager_moid, refusal = await locate(
            connector, ctx.operator, ctx.target, ctx.params.get("host"), manager
        )
        if refusal is not None:
            return {
                "summary": f"MEHO could not find the host ({refusal['status']}).",
                "requested": requested,
            }
        assert manager_moid is not None
        current = await read(connector, ctx.operator, ctx.target, manager_moid)
    except _READ_ERRORS as exc:
        return {
            "summary": "MEHO could not read the host, so it cannot show the current setting.",
            "requested": requested,
            "read_error": type(exc).__name__,
        }
    return {
        "summary": f"Change a security setting of host {host_moid}.",
        "host": host_moid,
        "current": current,
        "requested": requested,
    }

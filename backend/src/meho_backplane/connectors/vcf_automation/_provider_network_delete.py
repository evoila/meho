# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler and approval card of ``vcfa.provider.network.delete``.

Removes one networking object: regional networking, a provider gateway (its
IP space links go with it), an IP space, or one IP space link. The order
matters: regional networking first, then the gateway, then the IP space.
MEHO stops (``precondition_failed``) while something still uses the object:
regional networking that uses the gateway, or a link that uses the IP space.
Removing regional networking also removes the organization's NSX project,
transit gateway and default VPC.

The op is ``destructive``: an agent can never run it, and a person needs a
preview first. The preview builder reads the object and fills the
``blast_radius`` card (the object, what goes with it, and that it cannot be
undone); it refuses when the object does not exist or is still in use. The
card carries a hash of the object; the approved run reads the object again
and stops when it changed. VCF Automation answers the delete with HTTP 202 and
works on its own, so the run answers ``accepted`` with the vendor's task id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vcf_automation._provider_network import (
    KINDS,
    RUNNING_STATUSES,
    VcfaProviderNetworkError,
    drift_reason,
    job_id_of,
    list_kind,
    read_one,
    send,
    state_hash,
    status_read,
)
from meho_backplane.connectors.vcf_automation._provider_paths import bad_id_reason, provider_path
from meho_backplane.connectors.vcf_automation._provider_start import ASYNC_NOTE
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["DELETE_KINDS", "NetworkDeleteRefusedError", "provider_network_delete"]

DELETE_OP: Final = "vcfa.provider.network.delete"
#: Op ``kind`` -> the read kind of :data:`._provider_network.KINDS`.
DELETE_KINDS: Final = {
    "regional_networking": "regional_networking",
    "provider_gateway": "provider_gateways",
    "ip_space": "ip_spaces",
    "ip_space_association": "ip_space_associations",
}
_REGIONAL_CHILDREN: Final = (
    {"kind": "nsx_project", "what": "the organization's NSX project in this region"},
    {"kind": "transit_gateway", "what": "the organization's transit gateway in this region"},
    {"kind": "default_vpc", "what": "the organization's default VPC in this region"},
)


class NetworkDeleteRefusedError(VcfaProviderNetworkError):
    """The delete must not run; the message says why."""


async def _blockers(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    obj_id: str,
) -> list[dict[str, Any]]:
    """The objects that still use the object, as flat rows."""
    if kind == "provider_gateway":
        rows = await list_kind(
            connector, target, operator, "regional_networking", {"provider_gateway_id": obj_id}
        )
        return [
            {"kind": "regional_networking", **KINDS["regional_networking"].row(r)} for r in rows
        ]
    if kind == "ip_space":
        rows = await list_kind(
            connector, target, operator, "ip_space_associations", {"ip_space_id": obj_id}
        )
        return [
            {"kind": "ip_space_association", **KINDS["ip_space_associations"].row(r)} for r in rows
        ]
    return []


async def _children(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    kind: str,
    obj: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """What else goes with the object."""
    if kind == "regional_networking":
        return [dict(child) for child in _REGIONAL_CHILDREN]
    if kind == "provider_gateway":
        links = await list_kind(
            connector,
            target,
            operator,
            "ip_space_associations",
            {"provider_gateway_id": str(obj.get("id"))},
        )
        return [
            {"kind": "ip_space_link", "id": link.get("id"), "ip_space": row.get("ip_space_name")}
            for link in links
            for row in [KINDS["ip_space_associations"].row(link)]
        ]
    if kind == "ip_space":
        cidrs = KINDS["ip_spaces"].row(obj).get("cidrs") or []
        return [{"kind": "nsx_ip_block", "cidr": cidr} for cidr in cidrs]
    return []


def _object(kind: str, obj: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": kind, **KINDS[DELETE_KINDS[kind]].row(obj)}


def _blocked_reason(kind: str, blockers: list[dict[str, Any]]) -> str:
    names = [str(b.get("name") or b.get("id")) for b in blockers]
    first = "regional networking" if kind == "provider_gateway" else "IP space links"
    return (
        f"the {kind.replace('_', ' ')} is still used by {names}. Remove the {first} first "
        "(vcfa.provider.network.delete), then this one"
    )


async def provider_network_delete(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.network.delete`` — remove one networking object."""
    kind, obj_id = params["kind"], params["id"]
    read_kind = DELETE_KINDS[kind]
    base: dict[str, Any] = {
        "kind": kind,
        "object": None,
        "blockers": [],
        "job_id": None,
        "status_read": status_read(read_kind, id=obj_id),
        "guidance": None,
    }
    problem = bad_id_reason(obj_id, kind.replace("_", " "))
    if problem is not None:
        return {**base, "status": "invalid_request", "guidance": f"{problem}; nothing was deleted"}
    obj = await read_one(connector, target, operator, read_kind, obj_id)
    if obj is None:
        return {
            **base,
            "status": "already_absent",
            "guidance": "VCF Automation has no such object; nothing was deleted",
        }
    base = {**base, "object": _object(kind, obj)}
    drift = drift_reason(DELETE_OP, obj)
    if drift is not None:
        return {**base, "status": "precondition_failed", "guidance": drift}
    blockers = await _blockers(connector, target, operator, kind, obj_id)
    if blockers:
        reason = _blocked_reason(kind, blockers)
        return {
            **base,
            "status": "precondition_failed",
            "blockers": blockers,
            "guidance": f"{reason}; nothing was deleted",
        }
    if str(obj.get("status") or "").upper() in RUNNING_STATUSES:
        return {
            **base,
            "status": "already_running",
            "guidance": (
                f"VCF Automation still works on this object (status {obj.get('status')}); "
                "nothing was started. Read it until it ends, then preview the delete again"
            ),
        }
    spec = KINDS[read_kind]
    if spec.item is None or spec.item_param is None:
        raise VcfaProviderNetworkError(f"a {read_kind} object cannot be deleted by id")
    path = provider_path(target, spec.item, **{spec.item_param: obj_id})
    resp = await send(connector, target, operator, "DELETE", path)
    job_id = job_id_of(resp)
    return {
        **base,
        "status": "accepted",
        "job_id": job_id,
        "status_read": status_read(read_kind, id=obj_id, job_id=job_id),
        "guidance": (
            "VCF Automation accepted the delete; it is not done yet. Read the object with "
            "the status read: state not_found means it is gone, failed means it stays. "
            f"{ASYNC_NOTE}"
        ),
    }


async def _delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` card. Read-only; raises to refuse the delete.

    Declines (``None``) without a live connector: the egress-free preview has
    no card then, and the destructive gate refuses the call.
    """
    kind, obj_id = ctx.params.get("kind"), ctx.params.get("id")
    connector: Any = ctx.connector_instance
    if connector is None or ctx.target is None or kind not in DELETE_KINDS:
        return None
    problem = bad_id_reason(obj_id, str(kind).replace("_", " "))
    if problem is not None or not isinstance(obj_id, str):
        raise NetworkDeleteRefusedError(f"{problem}; nothing to delete")
    obj = await read_one(connector, ctx.target, ctx.operator, DELETE_KINDS[kind], obj_id)
    if obj is None:
        raise NetworkDeleteRefusedError(
            f"VCF Automation has no {kind.replace('_', ' ')} with id {obj_id!r}; nothing to "
            "delete. vcfa.provider.network.list shows what exists"
        )
    blockers = await _blockers(connector, ctx.target, ctx.operator, kind, obj_id)
    if blockers:
        raise NetworkDeleteRefusedError(_blocked_reason(kind, blockers))
    return {
        "action": "delete_network_object",
        "state_hash": state_hash(obj),
        "would": "delete",
        "blast_radius": {
            "object": _object(kind, obj),
            "children": await _children(connector, ctx.target, ctx.operator, kind, obj),
            "irreversibility": "permanent",
        },
    }


register_preview_builder(DELETE_OP, _delete_preview)

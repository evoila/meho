# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two typed reads: the local users of an ESXi host, and its Active Directory domain.

* ``vmware.host.local_user.list`` -- the local users with their access role.
  Needs the ESXi host itself as the target (local users live there, not on
  vCenter). ``UserDirectory.RetrieveUserGroups`` lists the users;
  ``HostAccessManager.RetrieveHostAccessControlEntries`` gives the role of
  each user. When the role read fails, the users still come back with
  ``access_mode: null`` and a line in ``read_notes``.
* ``vmware.host.ad.status`` -- is the host in a domain, which one, and is the
  membership healthy. Reads ``HostAuthenticationManager.info`` on a vCenter
  or an ESXi target.

The helpers are in :mod:`.host_access`, shared with the five changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.host_access import (
    AD_STATE_PROPERTIES,
    HOST_ACCESS_GROUP_KEY,
    HOST_ACCESS_WHEN_TO_USE,
    HOST_PARAMETER,
    needs_esxi_target,
    read_access_modes,
    read_account_managers,
    read_ad_state,
    read_local_users,
)
from meho_backplane.connectors.vmware_rest.host_target import STANDALONE_ESXI_HOST_MOID
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "HOST_ACCESS_GROUP_KEY",
    "HOST_ACCESS_WHEN_TO_USE",
    "VMWARE_HOST_ACCESS_OPS",
    "host_ad_status_impl",
    "host_local_user_list_impl",
]

_log = structlog.get_logger(__name__)

_NULL_STR: Final = {"type": ["string", "null"]}

ACCESS_MODES: Final = ["accessAdmin", "accessReadOnly", "accessNoAccess", "accessOther"]


# ---------------------------------------------------------------------------
# vmware.host.local_user.list
# ---------------------------------------------------------------------------


async def host_local_user_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.local_user.list``."""
    refusal = needs_esxi_target(target)
    if refusal is not None:
        return {"users": [], "read_notes": [], **refusal}
    managers = await read_account_managers(connector, target, operator)
    users = await read_local_users(connector, target, operator, managers.user_directory)
    notes: list[str] = []
    try:
        modes = await read_access_modes(connector, target, operator)
    except (httpx.HTTPError, RuntimeError) as exc:
        modes = {}
        notes.append(f"access roles: read failed with {type(exc).__name__}: {exc}")
    rows = [{**row, "access_mode": modes.get(row["user"])} for row in users]
    _log.info("vmware_host_local_users_read", target=target.name, users=len(rows))
    return {
        "status": "ok",
        "host": STANDALONE_ESXI_HOST_MOID,
        "users": rows,
        "read_notes": notes,
        "guidance": None,
    }


_USER_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user": {"type": "string", "description": "Login name of the local user."},
        "full_name": {**_NULL_STR, "description": "Full name or description, if set."},
        "uid": {"type": ["integer", "null"], "description": "POSIX user id."},
        "shell_access": {
            "type": ["boolean", "null"],
            "description": "Whether the user may use the ESXi shell.",
        },
        "access_mode": {
            "type": ["string", "null"],
            "enum": [*ACCESS_MODES, None],
            "description": (
                "Access role on the host: accessAdmin, accessReadOnly, accessNoAccess or "
                "accessOther. null when the user has no role or the role read failed."
            ),
        },
    },
    "required": ["user", "access_mode"],
}

_LOCAL_USER_LIST_OP = VmwareTypedOp(
    op_id="vmware.host.local_user.list",
    handler_attr="host_local_user_list",
    summary="The local users of one ESXi host and their access role.",
    description=(
        "Lists the local users of an ESXi host with name, full name, user id, shell access "
        "and access role. Needs the ESXi host itself as the target; a vCenter target answers "
        "needs_esxi_target. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "needs_esxi_target", "unsupported_host_target"],
                "description": "ok: the users were read; the others: nothing was read.",
            },
            "host": {**_NULL_STR, "description": "The host (ha-host on an ESXi target)."},
            "users": {
                "type": "array",
                "description": "One row per local user, sorted by name.",
                "items": _USER_ROW_SCHEMA,
            },
            "read_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One line per part that could not be read, with the reason.",
            },
            "target_product": {**_NULL_STR, "description": "Product on unsupported_host_target."},
            "guidance": {**_NULL_STR, "description": "What to do on a refusal."},
        },
        "required": ["status", "users", "read_notes"],
    },
    group_key=HOST_ACCESS_GROUP_KEY,
    tags=("read-only", "vmware", "esxi", "host", "users", "security"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see who can log in to an ESXi host directly: before a hardening check, "
            "an audit, or before you create, delete or change a local user."
        ),
        "output_shape": (
            "{status, host, users: [{user, full_name, uid, shell_access, access_mode}], "
            "read_notes, guidance}. access_mode is null for a user with no role on the host. "
            "A long list comes back as a result handle; read it with result_query."
        ),
        "next_step": (
            "To add a user, call vmware.composite.host.local_user.create; to change a "
            "password, vmware.composite.host.local_user.password.set; to remove one, "
            "vmware.composite.host.local_user.delete. On needs_esxi_target, call again with "
            "the ESXi host's own target (list_targets)."
        ),
    },
)


# ---------------------------------------------------------------------------
# vmware.host.ad.status
# ---------------------------------------------------------------------------


async def host_ad_status_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.ad.status``."""
    state, refusal = await read_ad_state(connector, target, operator, params.get("host"))
    if state is None:
        return {"joined": None, "domain": None, **(refusal or {})}
    _log.info("vmware_host_ad_status_read", target=target.name, host=state.host)
    return {"status": "ok", **state.as_fields()}


_HOST_REFUSALS: Final = [
    "host_required",
    "host_not_found",
    "ambiguous_host",
    "unsupported_host_target",
    "config_manager_unreadable",
]

_AD_STATUS_OP = VmwareTypedOp(
    op_id="vmware.host.ad.status",
    handler_attr="host_ad_status",
    summary="Is an ESXi host in an Active Directory domain, which one, and is it healthy.",
    description=(
        "Reads the Active Directory membership of one host: joined or not, the domain, the "
        "trusted domains and the health of the membership. Works on a vCenter target (name the "
        "host) and on an ESXi target. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"host": HOST_PARAMETER},
        "additionalProperties": False,
        "examples": [{"host": "<host-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", *_HOST_REFUSALS],
                "description": "ok: the host was read; the others: nothing was read.",
            },
            **AD_STATE_PROPERTIES,
            "candidate_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Host moids on ambiguous_host.",
            },
            "target_product": {**_NULL_STR, "description": "Product on unsupported_host_target."},
            "guidance": {**_NULL_STR, "description": "What to do on a refusal."},
        },
        "required": ["status", "joined", "domain"],
    },
    group_key=HOST_ACCESS_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "esxi", "host", "active-directory", "security"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check if a host is in Active Directory before a join or a leave, after "
            "one, or in a hardening or audit check."
        ),
        "parameter_hints": {"host": "Host name or moid, e.g. 'host-21'. Not needed on ESXi."},
        "output_shape": (
            "{status, host, joined, domain, trusted_domains, membership_status}. On a refusal "
            "status names why (host_required, host_not_found, ambiguous_host with "
            "candidate_hosts, ...) and nothing was read."
        ),
        "next_step": (
            "To join a domain, call vmware.composite.host.ad.join; to leave it, "
            "vmware.composite.host.ad.leave. On ambiguous_host, call again with one of "
            "candidate_hosts."
        ),
    },
)

VMWARE_HOST_ACCESS_OPS: Final[tuple[VmwareTypedOp, ...]] = (_LOCAL_USER_LIST_OP, _AD_STATUS_OP)

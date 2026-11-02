# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create, delete and set the password of a local user on an ESXi host.

Local users live on the host: ``HostLocalAccountManager`` (``CreateUser``,
``UpdateUser``, ``RemoveUser``) is on the ESXi service instance, not on
vCenter. So each change needs the ESXi host itself as the target; a vCenter
target stops with ``needs_esxi_target`` before any call.

Each change reads the users first, sends one gated call
(``_write_vmomi_sub_op``; SOAP on the host) and reads the users again:

* ``host.local_user.create`` -- ``already_exists`` sends nothing;
* ``host.local_user.password.set`` -- ``not_found`` sends nothing;
* ``host.local_user.delete`` -- ``unchanged`` (no such user) and ``protected``
  (``root``) send nothing. It is ``destructive``: its preview reads the user
  and fills the ``blast_radius``, and refuses a user that does not exist.

The password comes from Vault (``password_secret_ref``) and goes only into
the call to the host. The approval request, the preview and the answer name
the Vault path, never the password; a vendor text that holds it is replaced.
The create and the password change are pinned ``credential_write``
(``broadcast/events.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_PRECONDITION_FAILED,
    TeardownPlan,
    blast_radius,
)
from meho_backplane.connectors.vmware_rest.composites._write import _write_vmomi_sub_op
from meho_backplane.connectors.vmware_rest.host_access import (
    OP_CREATE_USER,
    OP_REMOVE_USER,
    OP_UPDATE_USER,
    PROTECTED_USERS,
    find_user,
    needs_esxi_target,
    read_access_modes,
    read_account_managers,
    read_local_users,
    vendor_fault,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import read_secret_param
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.operations._preview import preview_failure_reason, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OP_ID_LOCAL_USER_CREATE",
    "OP_ID_LOCAL_USER_DELETE",
    "OP_ID_LOCAL_USER_PASSWORD_SET",
    "local_user_create_composite",
    "local_user_delete_composite",
    "local_user_password_set_composite",
]

OP_ID_LOCAL_USER_CREATE: Final = "vmware.composite.host.local_user.create"
OP_ID_LOCAL_USER_DELETE: Final = "vmware.composite.host.local_user.delete"
OP_ID_LOCAL_USER_PASSWORD_SET: Final = "vmware.composite.host.local_user.password.set"

_ACCOUNT_SPEC_TYPE: Final = "HostAccountSpec"
_STATUS_FAILED: Final = "failed"


def _answer(
    status: str,
    user_name: str,
    user: dict[str, Any] | None,
    *,
    guidance: str | None = None,
    error: tuple[str | None, str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "status": status,
        "user_name": user_name,
        "user": _public_row(user),
        **extra,
        "error_type": error[0] if error else None,
        "error_message": error[1] if error else None,
        "guidance": guidance,
    }
    if error is not None:
        answer["issues"] = [issue("connector", "error", f"{error[0] or 'fault'}: {error[1]}")]
    return answer


def _public_row(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {key: user.get(key) for key in ("user", "full_name", "uid", "shell_access")}


def _not_confirmed(
    user_name: str, user: dict[str, Any] | None, status: str, text: str
) -> dict[str, Any]:
    answer = _answer(status, user_name, user, guidance=text)
    answer["issues"] = [issue("connector", "error", text)]
    return answer


async def _users(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> tuple[str, list[dict[str, Any]]]:
    managers = await read_account_managers(connector, target, operator)
    users = await read_local_users(connector, target, operator, managers.user_directory)
    return managers.account_manager, users


async def _send(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    plan: dict[str, Any],
    secrets: tuple[str, ...],
) -> tuple[OperationResult | None, tuple[str | None, str] | None]:
    """Gate and send one account call: ``(gate, None)``, ``(None, fault)`` or ``(None, None)``."""
    try:
        gate, _payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=plan,
        )
    except (httpx.HTTPStatusError, RuntimeError) as exc:
        fault = vendor_fault(exc, secrets)
        if fault is None:
            raise
        return None, fault
    return gate, None


def _plan(params: dict[str, Any], action: str) -> dict[str, Any]:
    """What the change does, without the password: for the approver and the gate."""
    plan: dict[str, Any] = {"action": action, "user_name": params["user_name"]}
    if params.get("password_secret_ref"):
        plan["password_source"] = "vault"
        plan["password_secret_ref"] = params["password_secret_ref"]
    if params.get("description"):
        plan["description"] = params["description"]
    return plan


# ---------------------------------------------------------------------------
# host.local_user.create
# ---------------------------------------------------------------------------


async def local_user_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a local user on the ESXi host (``HostLocalAccountManager.CreateUser``)."""
    name = params["user_name"]
    refusal = needs_esxi_target(target)
    if refusal is not None:
        return {**_answer(refusal["status"], name, None, guidance=refusal["guidance"]), **refusal}
    manager, users = await _users(connector, target, operator)
    existing = find_user(users, name)
    if existing is not None:
        return _answer(
            "already_exists",
            name,
            existing,
            guidance="The host already has this user. Nothing was sent. To change its "
            "password, use host.local_user.password.set.",
        )
    password = await read_secret_param(operator, target, params, prefix="password")
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: _ACCOUNT_SPEC_TYPE, "id": name, "password": password}
    if params.get("description"):
        spec["description"] = params["description"]
    gate, fault = await _send(
        connector,
        target,
        operator,
        op_id=OP_CREATE_USER,
        vmomi_path=f"/HostLocalAccountManager/{manager}/CreateUser",
        body={"user": spec},
        plan=_plan(params, "create the local user"),
        secrets=(password,),
    )
    if gate is not None:
        return gate
    if fault is not None:
        return _answer(
            _STATUS_FAILED,
            name,
            None,
            error=fault,
            guidance="The host refused the new user; nothing changed. Fix the cause the "
            "error names (for example a password the host refuses) and call again.",
        )
    _manager, after = await _users(connector, target, operator)
    created = find_user(after, name)
    if created is None:
        return _not_confirmed(
            name, None, "not_verified", "The host gave no error, but it lists no such user."
        )
    return _answer(
        "created",
        name,
        created,
        guidance="The user has no access role on the host yet.",
    )


# ---------------------------------------------------------------------------
# host.local_user.password.set
# ---------------------------------------------------------------------------


async def local_user_password_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set a local user's password (``HostLocalAccountManager.UpdateUser``)."""
    name = params["user_name"]
    refusal = needs_esxi_target(target)
    if refusal is not None:
        return {**_answer(refusal["status"], name, None, guidance=refusal["guidance"]), **refusal}
    manager, users = await _users(connector, target, operator)
    user = find_user(users, name)
    if user is None:
        return _answer(
            "not_found",
            name,
            None,
            guidance="The host has no local user with this name. Nothing was sent.",
        )
    password = await read_secret_param(operator, target, params, prefix="password")
    spec = {VIM_TYPE_NAME_KEY: _ACCOUNT_SPEC_TYPE, "id": name, "password": password}
    gate, fault = await _send(
        connector,
        target,
        operator,
        op_id=OP_UPDATE_USER,
        vmomi_path=f"/HostLocalAccountManager/{manager}/UpdateUser",
        body={"user": spec},
        plan=_plan(params, "set the password of the local user"),
        secrets=(password,),
    )
    if gate is not None:
        return gate
    if fault is not None:
        return _answer(
            _STATUS_FAILED,
            name,
            user,
            error=fault,
            guidance="The host refused the new password; the old one still works. Fix the "
            "cause the error names and call again.",
        )
    _manager, after = await _users(connector, target, operator)
    still = find_user(after, name)
    if still is None:
        return _not_confirmed(
            name, None, "not_found", "The host took the password, but now lists no such user."
        )
    return _answer("password_set", name, still)


# ---------------------------------------------------------------------------
# host.local_user.delete (destructive)
# ---------------------------------------------------------------------------


async def _plan_delete(
    connector: VmwareRestConnector, target: Any, operator: Operator, name: str
) -> tuple[TeardownPlan, str, dict[str, Any] | None]:
    """Read-only plan of a delete: the user, its role, and the refusal for root."""
    manager, users = await _users(connector, target, operator)
    user = find_user(users, name)
    plan = TeardownPlan(object={"kind": "local_user", "user": name}, present=user is not None)
    if user is None:
        return plan, manager, None
    plan.object.update(full_name=user.get("full_name"), uid=user.get("uid"))
    try:
        mode = (await read_access_modes(connector, target, operator)).get(name)
    except (httpx.HTTPError, RuntimeError) as exc:
        plan.object["access_mode_unread"] = preview_failure_reason(exc)
    else:
        if mode is not None:
            plan.children.append({"kind": "access_role", "user": name, "access_mode": mode})
    if name in PROTECTED_USERS:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"MEHO never deletes {name!r}: without it, nobody may be able to log in to the host.",
        )
    return plan, manager, user


async def local_user_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete a local user from the ESXi host (``HostLocalAccountManager.RemoveUser``)."""
    name = params["user_name"]
    refusal = needs_esxi_target(target)
    if refusal is not None:
        return {**_answer(refusal["status"], name, None, guidance=refusal["guidance"]), **refusal}
    plan, manager, user = await _plan_delete(connector, target, operator, name)
    if user is None:
        return _answer(
            "unchanged", name, None, guidance="The host has no such user. Nothing was sent."
        )
    if plan.refusal is not None:
        return _answer("protected", name, user, guidance=plan.refusal[1])
    gate, fault = await _send(
        connector,
        target,
        operator,
        op_id=OP_REMOVE_USER,
        vmomi_path=f"/HostLocalAccountManager/{manager}/RemoveUser",
        body={"userName": name},
        plan=_plan(params, "delete the local user"),
        secrets=(),
    )
    if gate is not None:
        return gate
    if fault is not None:
        return _answer(
            _STATUS_FAILED,
            name,
            user,
            error=fault,
            guidance="The host refused the delete; the user is still there.",
        )
    _manager, after = await _users(connector, target, operator)
    if find_user(after, name) is not None:
        return _not_confirmed(
            name, user, "still_present", "The host gave no error, but it still lists the user."
        )
    return _answer("deleted", name, user)


async def _delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of the delete. Raises when the user does not exist, so nothing waits."""
    name = ctx.params.get("user_name")
    if not isinstance(name, str) or ctx.connector_instance is None:
        return None
    refusal = needs_esxi_target(ctx.target)
    if refusal is not None:
        raise LookupError(refusal["guidance"])
    plan, _manager, user = await _plan_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        name,
    )
    if user is None:
        raise LookupError(f"the host has no local user {name!r}; there is nothing to delete")
    preview = blast_radius(plan)
    preview["blast_radius"]["what_happens_next"] = (
        "The host deletes the user and its access role. Whoever used this account cannot log "
        "in to the host any more. To undo it, create the user again with a new password."
    )
    return preview


# ---------------------------------------------------------------------------
# Previews of the create and the password change
# ---------------------------------------------------------------------------


async def _account_preview(ctx: PreviewContext, action: str) -> dict[str, Any] | None:
    """The plan (never the password) and the user as it is now. A failed read never blocks."""
    name = ctx.params.get("user_name")
    if not isinstance(name, str):
        return None
    preview: dict[str, Any] = _plan(dict(ctx.params), action)
    refusal = needs_esxi_target(ctx.target)
    if refusal is not None:
        preview["unavailable"] = [refusal["guidance"]]
        return preview
    if ctx.connector_instance is None:
        return preview
    try:
        _manager, users = await _users(
            ctx.connector_instance,  # type: ignore[arg-type]
            ctx.target,
            ctx.operator,
        )
    except Exception as exc:
        preview["unavailable"] = [f"the local users: {preview_failure_reason(exc)}"]
    else:
        preview["current_user"] = _public_row(find_user(users, name))
    return preview


async def _create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    return await _account_preview(ctx, "create the local user")


async def _password_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    return await _account_preview(ctx, "set the password of the local user")


register_preview_builder(OP_ID_LOCAL_USER_CREATE, _create_preview)
register_preview_builder(OP_ID_LOCAL_USER_PASSWORD_SET, _password_preview)
register_preview_builder(OP_ID_LOCAL_USER_DELETE, _delete_preview)

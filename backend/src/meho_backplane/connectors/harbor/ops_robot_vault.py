# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Store the new Harbor robot secret in Vault (``vault_target`` of ``harbor.robot.create``).

Harbor shows a robot's secret only once, in the answer to the create call.
MEHO hides secret fields from an agent, so an agent could never use it.
With the ``vault_target`` parameter, MEHO writes the secret to Vault itself
and returns only a keyed fingerprint and the length of the secret. Nobody
sees the secret: not the agent, not the approver, not the audit row.

``vault_target`` is ``vault:<path>#<field>``, the same form ``secret.move``
takes. The path is below the default KV v2 mount. The write merges the
field into the item, so the other fields of the item stay.

Order of work
=============

1. Before the Harbor call: check the form of ``vault_target`` and the
   tenant scope of the path. A bad target stops the call before a robot
   exists.
2. Before the call waits for approval: a params check refuses a bad form,
   and a permission preflight asks Vault whether the write will be denied
   (:func:`robot_vault_preflight`). A write that would be denied is refused
   before an approval card exists.
3. After the Harbor call: write the secret to Vault. When the write fails,
   MEHO deletes the new robot, because its secret would be lost.

No secret in an error
=====================

Errors name the exception class only. The Vault client message is never
passed on, and the exception chain is cut.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple

import hvac.exceptions
import structlog

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.vault import ops as _kv_ops
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    enforce_tenant_scope,
)
from meho_backplane.operations._preview import register_permission_preflight
from meho_backplane.operations._validate import params_problem, register_params_check

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.harbor.session import HarborTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "RobotVaultTarget",
    "RobotVaultTargetError",
    "check_robot_vault_target",
    "parse_vault_target",
    "robot_vault_preflight",
    "store_new_robot_secret",
]

_log = structlog.get_logger(__name__)

_CREATE_OP_ID = "harbor.robot.create"
_FORM = "vault:<path>#<field>"


class RobotVaultTargetError(ValueError):
    """``vault_target`` has a wrong form, or a path the caller may not write."""


class RobotVaultTarget(NamedTuple):
    """The Vault place the new robot secret goes to."""

    mount: str
    path: str
    field: str


def _parse_form(raw: Any) -> RobotVaultTarget:
    """Split ``vault:<path>#<field>``; raise :class:`RobotVaultTargetError` on a bad form."""
    if not isinstance(raw, str):
        raise RobotVaultTargetError(f"vault_target must be a string like {_FORM}")
    kind, colon, ref = raw.partition(":")
    path, hash_sign, field = ref.rpartition("#")
    path, field = path.strip(), field.strip()
    if kind != "vault" or not colon or not hash_sign or not path or not field:
        raise RobotVaultTargetError(
            f"vault_target must look like {_FORM}: a Vault KV path and the field "
            "that gets the secret"
        )
    if path.startswith("/") or path.endswith("/") or "//" in path or ".." in path.split("/"):
        raise RobotVaultTargetError(
            "vault_target path must be plain: no leading or trailing slash, no '//', no '..'"
        )
    try:
        mount, path = _kv_ops._extract_mount_and_path(
            _CREATE_OP_ID, {"mount": DEFAULT_KV_MOUNT, "path": path}
        )
    except _kv_ops.VaultPathShapeError as exc:
        raise RobotVaultTargetError(str(exc)) from None
    return RobotVaultTarget(mount, path, field)


def parse_vault_target(operator: Operator, params: dict[str, Any]) -> RobotVaultTarget | None:
    """The checked Vault place of *params*, or ``None`` when ``vault_target`` is not set.

    Checks the form and the tenant scope of the path. Raises
    :class:`RobotVaultTargetError` on a problem. Runs before any Harbor call.
    """
    raw = params.get("vault_target")
    if raw is None:
        return None
    dest = _parse_form(raw)
    try:
        enforce_tenant_scope(operator, mount=dest.mount, path=dest.path, read_only=False)
    except VaultTenantScopeError as exc:
        raise RobotVaultTargetError(str(exc)) from None
    return dest


def check_robot_vault_target(params: dict[str, Any]) -> dict[str, Any] | None:
    """Params check: refuse a ``vault_target`` of a wrong form before the call can park."""
    raw = params.get("vault_target")
    if raw is None:
        return None
    try:
        _parse_form(raw)
    except RobotVaultTargetError as exc:
        return params_problem("vault_target", _FORM, str(exc))
    return None


#: What the write needs: ``vault.kv.patch`` for an item that exists, and
#: ``vault.kv.put`` to create it when it does not.
_WRITE_CAPABILITIES = (
    _kv_ops.VAULT_KV_WRITE_CAPABILITIES["vault.kv.put"]
    | _kv_ops.VAULT_KV_WRITE_CAPABILITIES["vault.kv.patch"]
)


async def robot_vault_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time check: will Vault refuse the write of the new secret?

    Returns ``None`` without ``vault_target``. A path outside the tenant
    scope counts as denied. Otherwise asks Vault (``sys/capabilities-self``)
    for the same path and login the write uses (no Vault target: the default
    role). The answer holds capability names only, never a secret.
    """
    if ctx.params.get("vault_target") is None:
        return None
    try:
        dest = parse_vault_target(ctx.operator, ctx.params)
    except RobotVaultTargetError as exc:
        return {
            "check": "vault.capabilities-self",
            "will_be_denied": True,
            "reason": str(exc),
            "principal_sub": ctx.operator.sub,
        }
    if dest is None:
        return None
    return await _kv_ops.vault_capability_preflight(
        ctx.operator,
        _CREATE_OP_ID,
        _kv_ops.vault_kv_write_target_path(
            "vault.kv.put", {"mount": dest.mount, "path": dest.path}
        ),
        _WRITE_CAPABILITIES,
        None,
    )


async def _write(operator: Operator, dest: RobotVaultTarget, secret: str) -> dict[str, Any]:
    """Merge the secret into the KV item; create the item only when it is still missing.

    ``vault.kv.patch`` raises ``InvalidPath`` when the item does not exist.
    The ``vault.kv.put`` that then creates it runs with ``cas=0``: Vault
    writes only when the item still does not exist, so another writer's
    fields are never replaced.
    """
    kv_params = {"mount": dest.mount, "path": dest.path, "data": {dest.field: secret}}
    try:
        written = await _kv_ops.vault_kv_patch(operator, None, kv_params)
    except hvac.exceptions.InvalidPath:
        written = await _kv_ops.vault_kv_put(operator, None, {**kv_params, "cas": 0})
    return {
        "kind": "vault_kv",
        "mount": dest.mount,
        "path": dest.path,
        "field": dest.field,
        "version": written.get("version"),
    }


async def _undo(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    project: str,
    robot_id: int,
) -> str:
    """Delete the new robot; say what happened, for the error text."""
    try:
        await connector.robot_delete(operator, target, {"project": project, "id": robot_id})
    except Exception as exc:
        _log.warning("harbor_robot_undo_failed", robot_id=robot_id, error=type(exc).__name__)
        return (
            f"the new robot (id {robot_id}) could not be deleted ({type(exc).__name__}); "
            f"delete it with harbor.robot.delete before you try again"
        )
    return f"the new robot (id {robot_id}) was deleted"


async def store_new_robot_secret(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
    dest: RobotVaultTarget,
    created: dict[str, Any],
) -> dict[str, Any]:
    """Write the secret of the robot Harbor just made to *dest*; return no secret.

    *created* is Harbor's answer to the create call. When the write fails
    or is cancelled, the new robot is deleted (its secret would be lost) and
    the error says whether that worked.
    """
    material = SecretMaterial(str(created["secret"]))
    robot_id = int(created["id"])
    try:
        stored_to = await _write(operator, dest, str(created["secret"]))
    except BaseException as exc:
        undone = await asyncio.shield(
            _undo(connector, operator, target, str(params["project"]), robot_id)
        )
        if not isinstance(exc, Exception):
            raise
        raise RuntimeError(
            f"{_CREATE_OP_ID}: the new robot secret could not be written to Vault at "
            f"{dest.mount}/{dest.path} ({type(exc).__name__}); {undone}. Check that the "
            "write is allowed there and call the operation again."
        ) from None
    return {
        "id": robot_id,
        "name": created["name"],
        "status": "stored",
        "stored_to": stored_to,
        "value_fingerprint": material.fingerprint,
        "length": material.length,
    }


def _register() -> None:
    """Wire the params check and the permission preflight. Import-time."""
    register_params_check(_CREATE_OP_ID, check_robot_vault_target)
    register_permission_preflight(_CREATE_OP_ID, robot_vault_preflight)


_register()

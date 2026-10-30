# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The checks that run before a person is asked to approve a gcloud change.

Every workload identity change registers a ``register_permission_preflight``
check here:

* the pool delete, the provider create and the provider key update read
  the object they need (the pool, or the provider) with ``GET``, and then
  ask ``projects/<id>:testIamPermissions`` (Cloud Resource Manager) for
  the permission. The permission is checked on the project because
  Google's IAM API does not answer ``testIamPermissions`` on every pool;
* the pool create only asks the project for
  ``iam.workloadIdentityPools.create``;
* the binding add and removal ask ``testIamPermissions`` on the service
  account.

A missing pool, provider or service account (404), or a missing permission, sets
``will_be_denied``, so the dispatcher refuses the call before an approval
request is made. Any other failure skips the check; the call still waits
for approval.

Importing this module registers the checks. :mod:`.connector` imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.gcloud.session import GcloudTargetLike
from meho_backplane.connectors.gcloud.workload_identity import (
    _IAM_API,
    _pool_id,
    _pools_name,
    _service_account,
    _service_account_name,
)
from meho_backplane.connectors.gcloud.workload_identity_write import _provider_id
from meho_backplane.operations._preview import PreviewContext, register_permission_preflight

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

_CRM_API: Final[str] = "https://cloudresourcemanager.googleapis.com/v1"

_POOL_DELETE_PERMISSIONS: Final[tuple[str, ...]] = ("iam.workloadIdentityPools.delete",)
_POOL_CREATE_PERMISSIONS: Final[tuple[str, ...]] = ("iam.workloadIdentityPools.create",)
_PROVIDER_CREATE_PERMISSIONS: Final[tuple[str, ...]] = ("iam.workloadIdentityPoolProviders.create",)
_PROVIDER_UPDATE_PERMISSIONS: Final[tuple[str, ...]] = ("iam.workloadIdentityPoolProviders.update",)
_GET_CHECKS: Final[dict[str, str]] = {
    "pool": "gcloud.workloadIdentityPools.get",
    "provider": "gcloud.workloadIdentityPoolProviders.get",
}
_BINDING_PERMISSIONS: Final[tuple[str, ...]] = (
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
)


def _gcloud_connector(ctx: PreviewContext) -> GcloudConnector | None:
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

    connector = ctx.connector_instance
    if not isinstance(connector, GcloudConnector) or ctx.target is None:
        return None
    return connector


async def _permission_check(
    connector: GcloudConnector,
    target: GcloudTargetLike,
    operator: Operator,
    *,
    url: str,
    path: str,
    required: tuple[str, ...],
    missing_reason: str,
) -> dict[str, Any] | None:
    """Ask Google (``POST url``, a ``testIamPermissions`` call) whether the login holds *required*.

    Returns ``will_be_denied: True`` when a permission is missing or the
    resource does not exist (404), so MEHO does not ask a person to approve
    a call that cannot work. Any other failure returns ``None``: the check
    is skipped and the call still waits for approval.
    """
    check: dict[str, Any] = {
        "check": "gcloud.testIamPermissions",
        "path": path,
        "required": list(required),
    }
    try:
        payload = await connector._post_json_abs(
            target, url, operator=operator, json_body={"permissions": list(required)}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            return None
        return {**check, "granted": [], "will_be_denied": True, "reason": missing_reason}
    granted = sorted(p for p in payload.get("permissions") or [] if isinstance(p, str))
    return {**check, "granted": granted, "will_be_denied": not set(required) <= set(granted)}


async def _project_check(
    ctx: PreviewContext,
    *,
    required: tuple[str, ...],
    must_exist: tuple[str, str] | None = None,
) -> dict[str, Any] | None:
    """Optionally read an object, then ask the project whether the login holds *required*.

    *must_exist* is ``(kind, name)``: ``("pool", <pool name>)`` or
    ``("provider", <provider name>)``.

    Google's IAM API does not answer ``testIamPermissions`` on every pool,
    so the permission is checked on the project (Cloud Resource Manager)
    and the object's existence with a plain read. An object that does not
    exist (404) denies with ``<kind>_not_found``; a read that fails in another
    way skips the check.
    """
    connector = _gcloud_connector(ctx)
    if connector is None or ctx.target is None:
        return None
    extra: dict[str, Any] = {}
    if must_exist is not None:
        kind, name = must_exist
        try:
            await connector._get_json_abs(ctx.target, f"{_IAM_API}/{name}", operator=ctx.operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                return None
            return {
                "check": _GET_CHECKS[kind],
                "path": name,
                "required": list(required),
                "granted": [],
                "will_be_denied": True,
                "reason": f"{kind}_not_found",
            }
        extra = {kind: name}
    project = f"projects/{ctx.target.gcp_project}"
    check = await _permission_check(
        connector,
        ctx.target,
        ctx.operator,
        url=f"{_CRM_API}/{project}:testIamPermissions",
        path=project,
        required=required,
        missing_reason="project_not_found",
    )
    return None if check is None else {**check, **extra}


def _pool_name(ctx: PreviewContext) -> str | None:
    """The full name of the pool in ``ctx.params``; ``None`` for a bad value or no target."""
    if ctx.target is None:
        return None
    try:
        return f"{_pools_name(ctx.target)}/{_pool_id(ctx.params)}"
    except ValueError:
        return None


async def _pool_delete_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    pool = _pool_name(ctx)
    if pool is None:
        return None
    return await _project_check(ctx, required=_POOL_DELETE_PERMISSIONS, must_exist=("pool", pool))


async def _pool_create_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    if _pool_name(ctx) is None:
        return None
    return await _project_check(ctx, required=_POOL_CREATE_PERMISSIONS)


async def _provider_create_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    pool = _pool_name(ctx)
    if pool is None:
        return None
    return await _project_check(
        ctx,
        required=_PROVIDER_CREATE_PERMISSIONS,
        must_exist=("pool", pool),
    )


async def _provider_update_keys_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    pool = _pool_name(ctx)
    try:
        provider_id = _provider_id(ctx.params)
    except ValueError:
        return None
    if pool is None:
        return None
    return await _project_check(
        ctx,
        required=_PROVIDER_UPDATE_PERMISSIONS,
        must_exist=("provider", f"{pool}/providers/{provider_id}"),
    )


async def _binding_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    try:
        account = _service_account(ctx.params)
    except ValueError:
        return None
    connector = _gcloud_connector(ctx)
    if connector is None or ctx.target is None:
        return None
    resource = _service_account_name(ctx.target, account)
    return await _permission_check(
        connector,
        ctx.target,
        ctx.operator,
        url=f"{_IAM_API}/{resource}:testIamPermissions",
        path=resource,
        required=_BINDING_PERMISSIONS,
        missing_reason="service_account_not_found",
    )


_WIF: Final[str] = "gcloud.iam.workload_identity_pools"
register_permission_preflight(f"{_WIF}.delete", _pool_delete_preflight)
register_permission_preflight(f"{_WIF}.create", _pool_create_preflight)
register_permission_preflight(f"{_WIF}.providers.create", _provider_create_preflight)
register_permission_preflight(f"{_WIF}.providers.update_keys", _provider_update_keys_preflight)
# The binding add needs the same two permissions as the removal.
register_permission_preflight("gcloud.iam.service_accounts.binding.remove", _binding_preflight)
register_permission_preflight("gcloud.iam.service_accounts.binding.add", _binding_preflight)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The checks that run before a person is asked to approve a gcloud change.

Both writes of :mod:`meho_backplane.connectors.gcloud.workload_identity`
register a ``register_permission_preflight`` check here:

* the pool delete reads the pool (``GET``) and then asks
  ``projects/<id>:testIamPermissions`` (Cloud Resource Manager) for
  ``iam.workloadIdentityPools.delete``. The permission is checked on the
  project because Google's IAM API does not answer ``testIamPermissions``
  on every pool;
* the binding removal asks ``testIamPermissions`` on the service account.

A missing pool or service account (404), or a missing permission, sets
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
from meho_backplane.operations._preview import PreviewContext, register_permission_preflight

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

_CRM_API: Final[str] = "https://cloudresourcemanager.googleapis.com/v1"

_POOL_DELETE_OP_ID: Final[str] = "gcloud.iam.workload_identity_pools.delete"
_BINDING_REMOVE_OP_ID: Final[str] = "gcloud.iam.service_accounts.binding.remove"
_POOL_DELETE_PERMISSIONS: Final[tuple[str, ...]] = ("iam.workloadIdentityPools.delete",)
_BINDING_REMOVE_PERMISSIONS: Final[tuple[str, ...]] = (
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


async def _pool_delete_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Read the pool, then ask the project whether the login may delete pools.

    Google's IAM API does not answer ``testIamPermissions`` on every pool,
    so the permission is checked on the project (Cloud Resource Manager)
    and the pool's existence with a plain read. A pool that does not exist
    (404) denies with ``pool_not_found``; a read that fails in another way
    skips the check.
    """
    try:
        pool_id = _pool_id(ctx.params)
    except ValueError:
        return None
    connector = _gcloud_connector(ctx)
    if connector is None or ctx.target is None:
        return None
    pool_name = f"{_pools_name(ctx.target)}/{pool_id}"
    try:
        await connector._get_json_abs(ctx.target, f"{_IAM_API}/{pool_name}", operator=ctx.operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            return None
        return {
            "check": "gcloud.workloadIdentityPools.get",
            "path": pool_name,
            "required": list(_POOL_DELETE_PERMISSIONS),
            "granted": [],
            "will_be_denied": True,
            "reason": "pool_not_found",
        }
    project = f"projects/{ctx.target.gcp_project}"
    check = await _permission_check(
        connector,
        ctx.target,
        ctx.operator,
        url=f"{_CRM_API}/{project}:testIamPermissions",
        path=project,
        required=_POOL_DELETE_PERMISSIONS,
        missing_reason="project_not_found",
    )
    return None if check is None else {**check, "pool": pool_name}


async def _binding_remove_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
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
        required=_BINDING_REMOVE_PERMISSIONS,
        missing_reason="service_account_not_found",
    )


register_permission_preflight(_POOL_DELETE_OP_ID, _pool_delete_preflight)
register_permission_preflight(_BINDING_REMOVE_OP_ID, _binding_remove_preflight)

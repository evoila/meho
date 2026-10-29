# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for the workload identity and service-account policy operations.

The operation metadata is in
:mod:`meho_backplane.connectors.gcloud.ops_workload_identity`. This module
holds:

* the strict value checks. A pool ID, service account, role or member that
  does not match its pattern is refused before any call, so a value can
  never change the URL path or the policy in an unplanned way;
* :class:`WorkloadIdentityOps`, the mixin :class:`GcloudConnector` inherits.
  Its methods are the ``handler_attr`` targets of the five operations;
* the two checks that run before a person is asked to approve a change are
  in :mod:`meho_backplane.connectors.gcloud.workload_identity_preflight`.

The pool delete waits a short, bounded time for Google's operation to
finish. When it finishes, the result says ``DELETED``; when it fails, the
call fails; when it is still running after the wait, the result says
``DELETE_PENDING`` and how to check.

The binding removal is a read-change-write: it reads the policy, drops only
the named member from the named role (only in bindings without a
condition), and writes the policy back with the ``etag`` it read. When the
policy changed in between, Google answers ``409``; MEHO then refuses with
:class:`GcloudPolicyChangedError` and does not retry.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.gcloud.session import GcloudTargetLike

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

__all__ = [
    "MEMBER_PATTERN",
    "POOL_ID_PATTERN",
    "ROLE_PATTERN",
    "SERVICE_ACCOUNT_PATTERN",
    "GcloudPolicyChangedError",
    "WorkloadIdentityOps",
]

#: A pool ID: 4 to 32 characters of a-z, 0-9 and '-'; Google keeps 'gcp-' for itself.
POOL_ID_PATTERN: Final[str] = r"^(?!gcp-)[a-z0-9-]{4,32}$"

#: A service account e-mail or its numeric unique ID.
SERVICE_ACCOUNT_PATTERN: Final[str] = (
    r"^([a-z0-9][a-z0-9._-]{0,99}@[a-z0-9][a-z0-9.-]{0,99}\.gserviceaccount\.com|[0-9]{1,32})$"
)

#: A predefined role (roles/...) or a custom role of a project or organization.
ROLE_PATTERN: Final[str] = (
    r"^(roles/[A-Za-z0-9_.]{1,100}"
    r"|(projects|organizations)/[A-Za-z0-9_.:-]{1,100}/roles/[A-Za-z0-9_.]{1,100})$"
)

#: An IAM member as Google writes it in a policy.
MEMBER_PATTERN: Final[str] = (
    r"^(allUsers|allAuthenticatedUsers"
    r"|(deleted:)?(user|serviceAccount|group):[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+(\?uid=[0-9]+)?"
    r"|domain:[A-Za-z0-9.-]+"
    r"|principal(Set)?://iam\.googleapis\.com/[!-~]+)$"
)

_IAM_API: Final[str] = "https://iam.googleapis.com/v1"
_CRM_API: Final[str] = "https://cloudresourcemanager.googleapis.com/v1"

#: How often, and how many times at most, the pool delete asks Google
#: whether its operation has finished before it answers ``DELETE_PENDING``.
_OPERATION_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_OPERATION_POLL_TRIES: Final[int] = 15

#: Sleep. Tests replace it to move time forward without waiting.
_sleep = asyncio.sleep

#: How long Google keeps a deleted pool so it can be restored.
_RESTORE_DAYS: Final[int] = 30

_DELETE_NOTE: Final[str] = (
    "Google marks the pool deleted. It stops working now. Google keeps it for 30 days "
    "and it can be restored (undelete) in that time; after that it is gone for good. "
    "Its ID cannot be used for a new pool during those 30 days."
)

_PENDING_NOTE: Final[str] = (
    "Google accepted the delete but its operation had not finished when MEHO stopped "
    "waiting. The pool is not deleted yet. Check with "
    "gcloud.iam.workload_identity_pools.list (show_deleted=true): the pool is deleted "
    "when its state is DELETED. Google keeps a deleted pool for 30 days and it can be "
    "restored (undelete) in that time."
)

#: Google returns 409 (ABORTED) when the etag no longer matches; 412 is accepted too.
_ETAG_CONFLICT_STATUSES: Final[frozenset[int]] = frozenset({409, 412})

#: When Google answers with a policy version lower than 3, a binding with a
#: condition shows up under a renamed role that contains this text.
_HIDDEN_CONDITION_MARK: Final[str] = "_withcond_"


class GcloudPolicyChangedError(ConnectorUpstreamError):
    """Google refused a policy write because the policy changed after MEHO read it."""


# ---------------------------------------------------------------------------
# Value checks and paths
# ---------------------------------------------------------------------------


def _checked(params: dict[str, Any], key: str, pattern: str, what: str) -> str:
    """Return ``params[key]`` when it fully matches *pattern*, else raise ValueError."""
    value = params.get(key)
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(
            f"'{key}' must be {what}. MEHO refused the value before calling Google; "
            "nothing was changed."
        )
    return value


def _pool_id(params: dict[str, Any]) -> str:
    return _checked(params, "pool_id", POOL_ID_PATTERN, "a pool ID (4-32 characters a-z, 0-9, '-')")


def _service_account(params: dict[str, Any]) -> str:
    return _checked(
        params,
        "service_account_email",
        SERVICE_ACCOUNT_PATTERN,
        "a service account e-mail (...gserviceaccount.com) or numeric ID",
    )


def _pools_name(target: GcloudTargetLike) -> str:
    return f"projects/{target.gcp_project}/locations/global/workloadIdentityPools"


def _service_account_name(target: GcloudTargetLike, account: str) -> str:
    return f"projects/{target.gcp_project}/serviceAccounts/{account}"


def _connector(ops: WorkloadIdentityOps) -> GcloudConnector:
    """The connector these methods run on. ``WorkloadIdentityOps`` is only ever its base."""
    return cast("GcloudConnector", ops)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _delete_json_abs(
    connector: GcloudConnector, target: GcloudTargetLike, abs_url: str, *, operator: Operator
) -> dict[str, Any]:
    """DELETE an absolute GCP API URL; on a 401 refresh the token once and send again."""
    token = await connector._ensure_token(target, operator)
    client = await connector._http_client(target)
    resp = await client.delete(abs_url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        token = await connector.refresh_token(target)
        resp = await client.delete(abs_url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _list_all(
    connector: GcloudConnector,
    target: GcloudTargetLike,
    abs_url: str,
    *,
    operator: Operator,
    items_key: str,
    show_deleted: bool,
) -> list[dict[str, Any]]:
    """GET every page of a Google list call and return the items."""
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {"showDeleted": "true"} if show_deleted else {}
    while True:
        payload = await connector._get_json_abs(
            target, abs_url, operator=operator, params=query or None
        )
        items.extend(payload.get(items_key) or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items
        query["pageToken"] = page_token


async def _get_sa_policy(
    connector: GcloudConnector, target: GcloudTargetLike, account: str, *, operator: Operator
) -> dict[str, Any]:
    """Read the IAM policy of one service account, asking for version 3.

    For this call Google takes the version only as the query parameter
    ``options.requestedPolicyVersion``; the request body stays empty.
    """
    url = (
        f"{_IAM_API}/{_service_account_name(target, account)}:getIamPolicy"
        "?options.requestedPolicyVersion=3"
    )
    return await connector._post_json_abs(target, url, operator=operator)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _last_part(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _pool_row(pool: dict[str, Any]) -> dict[str, Any]:
    name = str(pool.get("name") or "")
    return {
        "pool_id": _last_part(name),
        "name": name,
        "display_name": pool.get("displayName"),
        "description": pool.get("description"),
        "state": pool.get("state"),
        "disabled": bool(pool.get("disabled", False)),
        "expire_time": pool.get("expireTime"),
    }


def _provider_row(provider: dict[str, Any]) -> dict[str, Any]:
    """One provider row. Key material (jwksJson, SAML metadata, trust store) is left out."""
    name = str(provider.get("name") or "")
    kind = next((k for k in ("oidc", "aws", "saml", "x509") if k in provider), None)
    oidc = provider.get("oidc") or {}
    aws = provider.get("aws") or {}
    return {
        "provider_id": _last_part(name),
        "name": name,
        "display_name": provider.get("displayName"),
        "description": provider.get("description"),
        "state": provider.get("state"),
        "disabled": bool(provider.get("disabled", False)),
        "kind": kind,
        "issuer_uri": oidc.get("issuerUri"),
        "allowed_audiences": list(oidc.get("allowedAudiences") or []),
        "aws_account_id": aws.get("accountId"),
        "attribute_mapping": dict(provider.get("attributeMapping") or {}),
        "attribute_condition": provider.get("attributeCondition"),
        "expire_time": provider.get("expireTime"),
    }


def _binding_rows(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": b.get("role", ""),
            "members": list(b.get("members") or []),
            "condition": b.get("condition"),
        }
        for b in policy.get("bindings") or []
    ]


def _without_member(
    bindings: list[dict[str, Any]], role: str, member: str
) -> tuple[list[dict[str, Any]], bool, bool, int]:
    """Drop *member* from the bindings of *role* that have no condition.

    Returns ``(new_bindings, removed, only_in_conditional, members_left)``.
    A binding left with no member is dropped. Bindings with a condition are
    never changed; ``only_in_conditional`` says the member holds the role
    there.
    """
    new_bindings: list[dict[str, Any]] = []
    removed = False
    in_conditional = False
    members_left = 0
    for binding in bindings:
        members = list(binding.get("members") or [])
        if binding.get("role") != role:
            new_bindings.append(binding)
            continue
        if binding.get("condition"):
            in_conditional = in_conditional or member in members
            new_bindings.append(binding)
            continue
        if member in members:
            members = [m for m in members if m != member]
            removed = True
        members_left += len(members)
        if members:
            new_bindings.append({**binding, "members": members})
    return new_bindings, removed, in_conditional and not removed, members_left


# ---------------------------------------------------------------------------
# Operation bodies
# ---------------------------------------------------------------------------


async def pools_list(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.list``."""
    pools = await _list_all(
        connector,
        target,
        f"{_IAM_API}/{_pools_name(target)}",
        operator=operator,
        items_key="workloadIdentityPools",
        show_deleted=bool(params.get("show_deleted", False)),
    )
    rows = [_pool_row(p) for p in pools]
    return {"rows": rows, "total": len(rows)}


async def providers_list(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.providers.list``."""
    pool_id = _pool_id(params)
    providers = await _list_all(
        connector,
        target,
        f"{_IAM_API}/{_pools_name(target)}/{pool_id}/providers",
        operator=operator,
        items_key="workloadIdentityPoolProviders",
        show_deleted=bool(params.get("show_deleted", False)),
    )
    rows = [_provider_row(p) for p in providers]
    return {"rows": rows, "total": len(rows)}


async def pool_delete(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.delete``: mark one pool deleted.

    Reads the pool first. A pool that is already marked deleted is not sent
    again, so the call is safe to run twice.
    """
    pool_id = _pool_id(params)
    name = f"{_pools_name(target)}/{pool_id}"
    url = f"{_IAM_API}/{name}"
    try:
        pool = await connector._get_json_abs(target, url, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ConnectorResourceNotFoundError(
                [name], f"Workload identity pool {pool_id!r} does not exist in this project."
            ) from exc
        raise
    result: dict[str, Any] = {
        "pool_id": pool_id,
        "name": name,
        "state": "DELETED",
        "already_deleted": pool.get("state") == "DELETED",
        "restorable_for_days": _RESTORE_DAYS,
        "operation_name": None,
        "operation_done": None,
        "note": _DELETE_NOTE,
    }
    if result["already_deleted"]:
        return result
    operation = await _delete_json_abs(connector, target, url, operator=operator)
    operation = await _wait_for_operation(connector, target, operation, operator=operator)
    _raise_if_operation_failed(operation, pool_id)
    result["operation_name"] = operation.get("name")
    result["operation_done"] = operation.get("done") is True
    if not result["operation_done"]:
        result["state"] = "DELETE_PENDING"
        result["note"] = _PENDING_NOTE
    return result


async def _wait_for_operation(
    connector: GcloudConnector,
    target: GcloudTargetLike,
    operation: dict[str, Any],
    *,
    operator: Operator,
) -> dict[str, Any]:
    """Poll a Google operation until it is done, or the bounded wait is used up.

    Returns the last state seen. The caller decides what a still-running
    operation means. An operation without a name cannot be polled and is
    returned as it is.
    """
    for _ in range(_OPERATION_POLL_TRIES):
        name = operation.get("name")
        if operation.get("done") is True or not isinstance(name, str) or not name:
            break
        await _sleep(_OPERATION_POLL_INTERVAL_SECONDS)
        operation = await connector._get_json_abs(target, f"{_IAM_API}/{name}", operator=operator)
    return operation


def _raise_if_operation_failed(operation: dict[str, Any], pool_id: str) -> None:
    error = operation.get("error")
    if not error:
        return
    message = error.get("message") if isinstance(error, dict) else str(error)
    raise ConnectorUpstreamError(
        f"Google could not delete workload identity pool {pool_id!r}.",
        # The DELETE itself succeeded; the operation it started failed.
        http_status=502,
        upstream_message=message if isinstance(message, str) else str(message),
    )


async def sa_policy_read(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.service_accounts.policy.read``."""
    account = _service_account(params)
    policy = await _get_sa_policy(connector, target, account, operator=operator)
    return {
        "service_account_email": account,
        "version": policy.get("version"),
        "etag": policy.get("etag"),
        "bindings": _binding_rows(policy),
    }


async def sa_binding_remove(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.service_accounts.binding.remove``: read, drop one member, write with etag."""
    account = _service_account(params)
    role = _checked(params, "role", ROLE_PATTERN, "a role such as roles/iam.workloadIdentityUser")
    member = _checked(params, "member", MEMBER_PATTERN, "an IAM member as written in the policy")

    policy = await _get_sa_policy(connector, target, account, operator=operator)
    if any(_HIDDEN_CONDITION_MARK in str(b.get("role", "")) for b in policy.get("bindings") or []):
        raise ValueError(
            "Google returned this policy in an older format that hides the conditions of some "
            "bindings (a role name contains '_withcond_'). MEHO cannot change such a policy "
            "safely; nothing was changed."
        )
    etag = policy.get("etag")
    new_bindings, removed, only_conditional, members_left = _without_member(
        list(policy.get("bindings") or []), role, member
    )
    result: dict[str, Any] = {
        "service_account_email": account,
        "role": role,
        "member": member,
        "removed": removed,
        "members_left": members_left,
        "etag": etag,
    }
    if only_conditional:
        raise ValueError(
            f"The member holds {role} on this service account only through a binding with "
            "a condition. MEHO does not change bindings with a condition; nothing was changed."
        )
    if not removed:
        return result
    if not etag:
        raise ValueError(
            "Google returned the policy without an etag, so MEHO cannot make a safe change. "
            "Nothing was changed."
        )

    url = f"{_IAM_API}/{_service_account_name(target, account)}:setIamPolicy"
    try:
        written = await connector._post_json_abs(
            target,
            url,
            operator=operator,
            json_body={"policy": {**policy, "bindings": new_bindings}},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _ETAG_CONFLICT_STATUSES:
            raise GcloudPolicyChangedError(
                f"The policy of service account {account!r} changed after MEHO read it, so "
                "Google refused the change (the etag no longer matches). MEHO changed nothing "
                "and did not retry. Read the policy again with "
                "gcloud.iam.service_accounts.policy.read and call again if the member is "
                "still there.",
                http_status=exc.response.status_code,
                upstream_message=None,
            ) from exc
        raise
    result["etag"] = written.get("etag")
    return result


class WorkloadIdentityOps:
    """The workload identity and service-account policy methods of ``GcloudConnector``."""

    async def gcloud_wif_pools_list(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.list``: the pools of the project."""
        return await pools_list(_connector(self), operator, target, params)

    async def gcloud_wif_providers_list(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.providers.list``: the providers of one pool."""
        return await providers_list(_connector(self), operator, target, params)

    async def gcloud_wif_pool_delete(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.delete``: mark one pool deleted."""
        return await pool_delete(_connector(self), operator, target, params)

    async def gcloud_sa_policy_read(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.service_accounts.policy.read``: the policy on one service account."""
        return await sa_policy_read(_connector(self), operator, target, params)

    async def gcloud_sa_binding_remove(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.service_accounts.binding.remove``: drop one member from one role."""
        return await sa_binding_remove(_connector(self), operator, target, params)

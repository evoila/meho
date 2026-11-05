# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure storage writes (#4169).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.storage.StorageOps`. Every function
here changes something in Azure, so every operation that runs one waits for
a person's approval.

* :func:`role_assignment_create_impl` gives a principal one of the two blob
  data roles (:data:`~.storage_schemas.STORAGE_DATA_ROLES`) on a storage
  account or one container. The assignment's name is derived from the
  scope, the principal and the role (:func:`assignment_name`), so the same
  call again sends the same ``PUT`` and makes no second assignment.
* :func:`role_assignment_delete_impl` removes every assignment of that
  principal and role at exactly that scope.

The scope is always built from the account MEHO found in the target's
subscription (:func:`~.storage_reads.find_account`), never from a path the
caller gave.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureConflictError, AzureNotFoundError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    AUTHORIZATION_API_VERSION,
    ROLE_ASSIGNMENT_PATH,
    ROLE_ASSIGNMENTS_PATH,
)
from meho_backplane.connectors.azure.session import (
    AzureTargetConfig,
    AzureTargetLike,
    resolve_target_config,
)
from meho_backplane.connectors.azure.storage_parse import (
    StorageAccount,
    StorageReadError,
    account_name,
    container_name,
)
from meho_backplane.connectors.azure.storage_reads import find_account
from meho_backplane.connectors.azure.storage_schemas import STORAGE_DATA_ROLES

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "assignment_name",
    "role_assignment_create_impl",
    "role_assignment_delete_impl",
    "storage_scope",
]

#: The namespace of :func:`assignment_name`. Fixed forever: a new value would
#: give every existing assignment a new name, and a second ``create`` would
#: then answer "already assigned" instead of "created".
_ASSIGNMENT_NAMESPACE: Final[uuid.UUID] = uuid.UUID("4f3c8a52-1d6e-4b7a-9c0e-41690000a5e1")

#: Only the assignments made at the scope itself, not those inherited from above.
_AT_SCOPE: Final[str] = "atScope()"

#: A role assignment name as Azure makes it. A name from Azure goes into a
#: ``DELETE`` path only when it looks like this.
_ASSIGNMENT_NAME: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)


def assignment_name(scope: str, principal_id: str, role_guid: str) -> str:
    """The role assignment name (a GUID) of *principal_id* having *role_guid* at *scope*.

    Case does not matter: Azure treats scopes and ids without regard to case.
    """
    key = f"{scope.lower()}|{principal_id.lower()}|{role_guid.lower()}"
    return str(uuid.uuid5(_ASSIGNMENT_NAMESPACE, key))


def storage_scope(config: AzureTargetConfig, account: StorageAccount, container: str | None) -> str:
    """The scope of an account, or of one of its containers, with its first slash."""
    if not account.resource_group:
        raise StorageReadError(
            f"Azure did not say which resource group holds the storage account {account.name!r}.",
            remediation="Check the account with azure.storage.account.list.",
        )
    scope = (
        f"/subscriptions/{config.subscription_id}/resourceGroups/{account.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account.name}"
    )
    if container:
        scope += f"/blobServices/default/containers/{container}"
    return scope


def _role_definition_id(config: AzureTargetConfig, role_guid: str) -> str:
    return (
        f"/subscriptions/{config.subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{role_guid}"
    )


def _role_guid(role: object) -> str:
    guid = STORAGE_DATA_ROLES.get(role) if isinstance(role, str) else None
    if guid is None:
        raise StorageReadError(
            f"MEHO gives only these roles: {', '.join(sorted(STORAGE_DATA_ROLES))}.",
            remediation="Pass one of these role names.",
        )
    return guid


async def _scope_of(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> str:
    name = account_name(params.get("account"), target.name)
    container = params.get("container")
    checked = container_name(container, target.name) if container else None
    account = await find_account(connector, operator, target, name)
    return storage_scope(resolve_target_config(target), account, checked)


async def _matching(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    scope: str,
    principal_id: str,
    role_guid: str,
) -> list[dict[str, Any]]:
    """The assignments made at *scope* itself that give *principal_id* the role."""
    paged = await list_all_pages(
        connector,
        target,
        ROLE_ASSIGNMENTS_PATH.format(scope=scope.lstrip("/")),
        operator=operator,
        api_version=AUTHORIZATION_API_VERSION,
        params={"$filter": _AT_SCOPE},
    )
    found: list[dict[str, Any]] = []
    for item in paged.rows:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        principal = str(properties.get("principalId") or "").lower()
        role_id = str(properties.get("roleDefinitionId") or "").lower()
        if principal == principal_id.lower() and role_id.endswith(f"/{role_guid.lower()}"):
            found.append(item)
    return found


async def role_assignment_create_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.role.assignment.create``: give a principal a blob data role.

    ``PUT`` of the derived name: Azure answers 201 for a new assignment and
    200 when this name already exists. When the principal already has the
    role under another name (made outside MEHO), Azure answers 409; MEHO
    then reads the assignments at the scope and answers ``already_assigned``
    with the existing id.
    """
    role = params.get("role")
    role_guid = _role_guid(role)
    principal_id = str(params["principal_id"])
    config = resolve_target_config(target)
    scope = await _scope_of(connector, operator, target, params)
    name = assignment_name(scope, principal_id, role_guid)
    body = {
        "properties": {
            "roleDefinitionId": _role_definition_id(config, role_guid),
            "principalId": principal_id,
            "principalType": params["principal_type"],
        }
    }
    answer = {"scope": scope, "principal_id": principal_id, "role": role}
    try:
        response = await send(
            connector,
            target,
            "PUT",
            ROLE_ASSIGNMENT_PATH.format(scope=scope.lstrip("/"), name=name),
            operator=operator,
            params={"api-version": AUTHORIZATION_API_VERSION},
            json=body,
        )
    except AzureConflictError:
        existing = await _matching(connector, operator, target, scope, principal_id, role_guid)
        if not existing:
            raise
        return {**answer, "assignment_id": existing[0].get("id"), "state": "already_assigned"}
    payload = json_payload_or_empty(response)
    assignment_id = payload.get("id") if isinstance(payload, dict) else None
    return {
        **answer,
        "assignment_id": assignment_id
        or f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}",
        "state": "created",
    }


async def role_assignment_delete_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.role.assignment.delete``: remove a principal's blob data role at a scope.

    Reads the assignments made at the scope itself and deletes every one of
    that principal and role, whatever its name. Azure answers 204 (or 404)
    when an assignment is already gone; that id goes to ``not_found``.
    """
    role = params.get("role")
    role_guid = _role_guid(role)
    principal_id = str(params["principal_id"])
    scope = await _scope_of(connector, operator, target, params)
    deleted: list[str] = []
    not_found: list[str] = []
    for item in await _matching(connector, operator, target, scope, principal_id, role_guid):
        assignment_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not _ASSIGNMENT_NAME.fullmatch(name):
            continue
        try:
            response = await send(
                connector,
                target,
                "DELETE",
                ROLE_ASSIGNMENT_PATH.format(scope=scope.lstrip("/"), name=name),
                operator=operator,
                params={"api-version": AUTHORIZATION_API_VERSION},
            )
        except AzureNotFoundError:
            not_found.append(assignment_id)
            continue
        (not_found if response.status_code == 204 else deleted).append(assignment_id)
    return {
        "scope": scope,
        "principal_id": principal_id,
        "role": role,
        "deleted": deleted,
        "not_found": not_found,
    }

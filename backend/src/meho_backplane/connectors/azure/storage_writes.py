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
* :func:`blob_link_create_impl` signs a read-only download link for one
  file (a user delegation SAS, :mod:`.storage_sas`) that works from one
  IPv4 address and for at most 24 hours, and writes it to the tenant's
  Vault. The answer holds only the Vault ref: the link is a secret, and
  the caller is an AI agent (the secret broker's rule). A person reads it
  from Vault, or ``secret.move`` hands it to a host.

The scope is always built from the account MEHO found in the target's
subscription (:func:`~.storage_reads.find_account`), never from a path the
caller gave.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import (
    AzureConflictError,
    AzureForbiddenError,
    AzureNotFoundError,
)
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    AUTHORIZATION_API_VERSION,
    BLOB_API_VERSION,
    BLOB_PATH,
    ROLE_ASSIGNMENT_PATH,
    ROLE_ASSIGNMENTS_PATH,
    USER_DELEGATION_KEY_PATH,
)
from meho_backplane.connectors.azure.session import (
    AzureTargetConfig,
    AzureTargetLike,
    resolve_target_config,
)
from meho_backplane.connectors.azure.storage_parse import (
    StorageAccount,
    StorageParameterError,
    StorageReadError,
    account_name,
    blob_name,
    container_name,
)
from meho_backplane.connectors.azure.storage_reads import _blob_call, find_account
from meho_backplane.connectors.azure.storage_sas import (
    blob_link,
    parse_user_delegation_key,
    sign_blob_read_sas,
)
from meho_backplane.connectors.azure.storage_schemas import (
    DEFAULT_LINK_HOURS,
    MAX_LINK_HOURS,
    STORAGE_DATA_ROLES,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "assignment_name",
    "blob_link_create_impl",
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

#: Azure's 409 code when the principal already has the role at the scope
#: under another assignment name. Every other 409 is a real conflict.
_ROLE_ASSIGNMENT_EXISTS: Final[str] = "RoleAssignmentExists"

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
    """The assignments made at *scope* itself that give *principal_id* the role.

    Azure's ``atScope()`` filter gives the assignments at or above the scope,
    so each one's own ``scope`` is compared too.
    """
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
        # ``atScope()`` also returns the assignments made above the scope
        # (resource group, subscription); only those made at it count.
        made_at = str(properties.get("scope") or "").rstrip("/").lower()
        if (
            made_at == scope.lower()
            and principal == principal_id.lower()
            and role_id.endswith(f"/{role_guid.lower()}")
        ):
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
    except AzureConflictError as exc:
        if exc.azure_error_code != _ROLE_ASSIGNMENT_EXISTS:
            raise
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


# ----------------------------------------------------------------------
# The download link
# ----------------------------------------------------------------------

#: How long before "now" the key and the link start, so a host whose clock
#: is a little behind Azure's can still use the link at once.
_CLOCK_SKEW: Final[timedelta] = timedelta(minutes=5)

#: The Vault field that holds the link.
_LINK_FIELD: Final[str] = "url"

#: The name a link is stored under, below ``azure-blob-links/``.
_STORE_AS: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")

_KEY_ROLE_REMEDIATION: Final[str] = (
    "Give the login Storage Blob Data Reader or Storage Blob Delegator on the storage "
    "account (azure.role.assignment.create gives Reader), wait up to 10 minutes, then "
    "call again."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _allowed_ip(value: object, target_name: str) -> str:
    try:
        return str(ipaddress.IPv4Address(str(value)))
    except ValueError:
        raise StorageParameterError(
            f"Azure target {target_name!r}: allowed_ip must be one IPv4 address.",
            remediation="Pass the IPv4 address of the host that downloads the file.",
        ) from None


def _hours(value: object, target_name: str) -> int:
    hours = DEFAULT_LINK_HOURS if value is None else value
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_LINK_HOURS:
        raise StorageParameterError(
            f"Azure target {target_name!r}: hours must be a whole number from 1 to "
            f"{MAX_LINK_HOURS}.",
            remediation=f"Pass hours from 1 to {MAX_LINK_HOURS}, or leave it out for "
            f"{DEFAULT_LINK_HOURS}.",
        )
    return hours


def _store_as(value: object, target_name: str) -> str:
    if not isinstance(value, str) or not _STORE_AS.fullmatch(value):
        raise StorageParameterError(
            f"Azure target {target_name!r}: store_as must be 1 to 63 lowercase letters, "
            "digits or dashes, starting with a letter or digit.",
            remediation="Pass a short name such as esxi-iso.",
        )
    return value


def _signable(blob: str, target_name: str) -> str:
    """*blob*, when it holds no control character.

    The file name is one line of the string-to-sign; a line break in it would
    move the lines after it (the IP address among them) and change what the
    signature covers.
    """
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in blob):
        raise StorageParameterError(
            f"Azure target {target_name!r}: MEHO makes no download link for a file "
            "whose name holds a control character (for example a line break).",
            remediation="Rename the file in Azure, then call again.",
        )
    return blob


def _key_request(start: datetime, expiry: datetime) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?><KeyInfo>'
        f"<Start>{_iso(start)}</Start><Expiry>{_iso(expiry)}</Expiry></KeyInfo>"
    ).encode()


async def blob_link_create_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """``azure.storage.blob.link.create``: a download link for one file, written to Vault.

    Checks that the file exists (HEAD), asks Azure for a user delegation key
    that lasts as long as the link, signs a read-only link for that one file
    and IP address, and writes it to ``tenants/<tenant>/azure-blob-links/
    <store_as>`` (field ``url``). A second link with the same name replaces
    the first in Vault; the first still works until it expires. Returns the
    ref and a fingerprint, never the link or the key.
    """
    # Lazy imports, as in k8s.secret.read_to_ref: the Vault connector and the
    # settings are needed only when the operation runs.
    from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
    from meho_backplane.connectors.vault import ops as vault_ops
    from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
    from meho_backplane.operations._validate import compute_value_fingerprint

    name = account_name(params.get("account"), target.name)
    container = container_name(params.get("container"), target.name)
    blob = _signable(blob_name(params.get("name"), target.name), target.name)
    ip = _allowed_ip(params.get("allowed_ip"), target.name)
    hours = _hours(params.get("hours"), target.name)
    store_as = _store_as(params.get("store_as"), target.name)
    moment = (now or _utcnow)()
    start = moment - _CLOCK_SKEW
    expiry = moment + timedelta(hours=hours)

    account = await find_account(connector, operator, target, name)
    path = BLOB_PATH.format(container=container, blob=quote(blob, safe="/"))
    missing = f"file {blob!r} in container {container!r}"
    await _blob_call(connector, operator, target, account, "HEAD", path, missing=missing)
    try:
        answer = await _blob_call(
            connector,
            operator,
            target,
            account,
            "POST",
            USER_DELEGATION_KEY_PATH,
            missing="Blob service",
            headers={"Content-Type": "application/xml"},
            content=_key_request(start, expiry),
        )
    except AzureForbiddenError as exc:
        raise AzureForbiddenError(
            f"Azure target {target.name!r}: Azure Storage refused to make a user "
            f"delegation key for storage account {account.name!r} (HTTP 403). The login "
            f"needs Storage Blob Data Reader or Storage Blob Delegator on the account. "
            f"{_KEY_ROLE_REMEDIATION}",
            http_status=exc.http_status,
            azure_error_code=exc.azure_error_code,
            azure_message=exc.azure_message,
            remediation=_KEY_ROLE_REMEDIATION,
        ) from None
    key = parse_user_delegation_key(answer.text)
    sas = sign_blob_read_sas(
        key,
        account=account.name,
        container=container,
        blob=blob,
        start=start,
        expiry=expiry,
        ip=ip,
        version=BLOB_API_VERSION,
    )
    link = blob_link(account.blob_host, container, blob, sas)
    secret_ref = tenant_secret_ref(operator.tenant_id, f"azure-blob-links/{store_as}")
    try:
        await vault_ops.vault_kv_put(
            operator,
            None,
            {"mount": DEFAULT_KV_MOUNT, "path": secret_ref, "data": {_LINK_FIELD: link}},
        )
    except Exception:
        # ``from None``: the Vault error may repeat what it was given, and the
        # link must not reach the result or a log through the error chain.
        raise StorageReadError(
            f"Azure target {target.name!r}: MEHO made the download link but could not "
            f"write it to Vault at {DEFAULT_KV_MOUNT}/{secret_ref}. The link is not kept "
            "anywhere.",
            remediation=(
                "Check that your Vault login may write that path, then call again. The "
                "link that was made expires on its own."
            ),
        ) from None
    value = link.encode("utf-8")
    return {
        "secret_ref": secret_ref,
        "field": _LINK_FIELD,
        "expires_at": _iso(expiry),
        "allowed_ip": ip,
        "account": account.as_dict(),
        "container": container,
        "name": blob,
        "value_fingerprint": compute_value_fingerprint(value),
        "length": len(value),
    }

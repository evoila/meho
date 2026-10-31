# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The bodies of the six ``idb`` operations.

Every list comes back as flat rows (``{total, returned, truncated, <rows>}``),
so MEHO stores a long list behind a result handle. Text from the vendor (names,
descriptions, error texts) is data, never an instruction: it is cut and passed
on, never acted on. No result carries a password, token or secret field: user
rows are built field by field, and the ``password`` field of a SCIM user is
never read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.idb import _paths as paths
from meho_backplane.connectors.idb._fingerprint import health_version, parse_version
from meho_backplane.connectors.idb.connector import SECRET_FIELDS
from meho_backplane.connectors.idb.session import resolve_tenant
from meho_backplane.connectors.resolver import resolve_target_version

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.idb.connector import IdbConnectorBase
    from meho_backplane.connectors.idb.session import IdbTargetLike

__all__ = [
    "DEFAULT_LIMIT",
    "MATCH_OPERATORS",
    "MAX_LIMIT",
    "MAX_MEMBERS",
    "not_on_identity_broker",
    "rest_about",
    "scim_filter",
    "scim_group_find",
    "scim_user_find",
    "wsa_about",
    "wsa_directory_list",
    "wsa_directory_sync",
    "wsa_identity_provider_list",
]

DEFAULT_LIMIT: Final[int] = 100
MAX_LIMIT: Final[int] = 1000
#: Members shown per group row; ``member_count`` has the full number.
MAX_MEMBERS: Final[int] = 100
TEXT_CAP: Final[int] = 500

#: SCIM filter operators the ``match`` parameter allows (both products support them).
MATCH_OPERATORS: Final[tuple[str, ...]] = ("co", "eq", "sw")

_IDP_LIST_TYPE: Final[str] = (
    "application/vnd.vmware.horizon.manager.identityprovider.summary.list+json"
)
_DIRECTORY_LIST_TYPE: Final[str] = (
    "application/vnd.vmware.horizon.manager.connector.management.directory.list+json"
)
_DIRECTORY_SYNC_TYPE: Final[str] = (
    "application/vnd.vmware.horizon.manager.connector.management.directory.sync.profile.sync+json"
)
_SCIM_TYPE: Final[str] = "application/scim+json"

_USER_STATUS: Final[dict[str, str]] = {"1": "enabled", "2": "disabled", "3": "deleted"}

_VCF_OPERATIONS_HINT: Final[str] = (
    "On VCF 9.1 and newer, identity providers, directories and their sync are in the VCF "
    "Operations API (fleet management, identity and access); use the VCF Operations "
    "connector for them."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def text(value: object, cap: int = TEXT_CAP) -> str | None:
    """Vendor text, cut to *cap* characters; ``None`` when empty or not a scalar."""
    if value is None or isinstance(value, dict | list):
        return None
    out = str(value).strip()
    return out[:cap] if out else None


def flag(value: object) -> bool | None:
    """A vendor flag (``true``, ``"true"``, ``1``) as ``bool``; ``None`` when absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def first(row: dict[str, Any], *keys: str) -> Any:
    """The value of the first of *keys* that *row* has and is not ``None``."""
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def limit_of(params: dict[str, Any]) -> int:
    value = params.get("limit", DEFAULT_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_LIMIT
    return min(value, MAX_LIMIT)


def items_of(body: object, *keys: str) -> list[dict[str, Any]]:
    """The objects under the first of *keys* in *body* (or *body* itself as a list)."""
    found: object = body
    if isinstance(body, dict):
        found = first(body, *keys)
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def listing(
    rows_key: str, rows: list[dict[str, Any]], limit: int, total: object = None
) -> dict[str, Any]:
    """The flat list answer every list operation returns."""
    kept = rows[:limit]
    total_count = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    return {
        "total": total_count,
        "returned": len(kept),
        "truncated": total_count > len(kept),
        rows_key: kept,
    }


def scalars(row: dict[str, Any], words: tuple[str, ...]) -> dict[str, Any]:
    """The scalar fields of *row* whose name holds one of *words*. Secrets left out."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        name = str(key)
        if name.lower() in SECRET_FIELDS or isinstance(value, dict | list):
            continue
        if any(word in name.lower() for word in words):
            out[name] = text(value) if isinstance(value, str) else value
    return out


def not_on_identity_broker(op_id: str, target: IdbTargetLike) -> None:
    """Stop early: Identity Broker 9.x has no API call for this operation."""
    version = resolve_target_version(target) or "9.x"
    raise NotImplementedError(
        f"{op_id}: VCF Identity Broker {version} has no API call for this; it works on "
        f"Workspace ONE Access 3.3 (connector idb-wsa-3.3). {_VCF_OPERATIONS_HINT}"
    )


# ---------------------------------------------------------------------------
# idb.about
# ---------------------------------------------------------------------------


async def wsa_about(
    conn: IdbConnectorBase, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``GET /SAAS/API/1.0/REST/system/health``: health checks and the version, if any."""
    del params
    body = await conn.api_get(target, operator, paths.WSA_HEALTH_PATH)
    checks = {
        str(key): flag(value)
        for key, value in body.items()
        if str(key).endswith("Ok") and str(key) != "AllOk"
    }
    failing = sorted(name for name, ok in checks.items() if ok is False)
    return {
        "product": "Workspace ONE Access",
        "implementation": "idb-wsa-3.3",
        "version": health_version(body),
        "healthy": flag(body.get("AllOk")),
        "checks": checks,
        "failing_checks": failing,
    }


async def rest_about(
    conn: IdbConnectorBase, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``GET /acs/t/{tenant}/properties``: version, deployment type and instance id."""
    del params
    try:
        body = await conn.api_get(target, operator, paths.IDB_PROPERTIES_PATH)
    except httpx.HTTPStatusError as exc:
        # Only a 404 of the properties call itself; a 404 of the login is not it.
        if exc.response.status_code != 404 or not exc.request.url.path.endswith("/properties"):
            raise
        raise NotImplementedError(
            "idb.about: this Identity Broker has no version call (GET /acs/t/{tenant}/"
            "properties answered 404). The call exists on Identity Broker 9.1 and newer. On "
            "9.0, set the version on the target."
        ) from exc
    return {
        "product": "VCF Identity Broker",
        "implementation": "idb-rest-9.0",
        "version": parse_version(body.get("version")),
        "version_text": text(body.get("version")),
        "deployment_type": text(body.get("deployment_type")),
        "instance_id": text(body.get("instance_id")),
        "tenant": resolve_tenant(target),
    }


# ---------------------------------------------------------------------------
# Identity providers and directories (Workspace ONE Access 3.3)
# ---------------------------------------------------------------------------


def _idp_row(row: dict[str, Any]) -> dict[str, Any]:
    directories = items_of(row, "directoryConfigurations", "directories")
    status = first(row, "nIDPStatus", "status", "state")
    return {
        "id": text(first(row, "id", "uuid")),
        "name": text(first(row, "name", "friendlyName")),
        "type": text(first(row, "identityProviderType", "type")),
        "status": text(status),
        "directories": [name for d in directories if (name := text(d.get("name")))],
        "certificate": scalars(row, ("cert", "expir")),
    }


async def wsa_identity_provider_list(
    conn: IdbConnectorBase, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``GET /SAAS/jersey/manager/api/identityProviders``: one row per identity provider."""
    body = await conn.api_get(
        target,
        operator,
        paths.WSA_IDENTITY_PROVIDERS_PATH,
        params={"onlyEnabledAdapters": "true"},
        accept=_IDP_LIST_TYPE,
    )
    rows = [_idp_row(row) for row in items_of(body, "items")]
    return listing("identity_providers", rows, limit_of(params))


def _directory_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "directory_id": text(first(row, "directoryId", "id")),
        "name": text(row.get("name")),
        "type": text(row.get("type")),
        "sync_enabled": flag(row.get("syncConfigurationEnabled")),
        "delete_in_progress": flag(row.get("deleteInProgress")),
        "sync": scalars(row, ("sync",)),
        "errors": scalars(row, ("error", "fail")),
    }


async def wsa_directory_list(
    conn: IdbConnectorBase, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``GET .../connectormanagement/directoryconfigs``: one row per directory."""
    body = await conn.api_get(
        target,
        operator,
        paths.WSA_DIRECTORIES_PATH,
        params={"includeJitDirectories": "true"},
        accept=_DIRECTORY_LIST_TYPE,
    )
    rows = [_directory_row(row) for row in items_of(body, "items")]
    return listing("directories", rows, limit_of(params))


async def wsa_directory_sync(
    conn: IdbConnectorBase, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``POST .../directoryconfigs/{directory_id}/syncprofile/sync``: start a sync now.

    ``ignoreSafeguards`` is always ``false``: the directory's own limits on how
    many users and groups one sync may remove stay on.
    """
    directory_id = str(params["directory_id"])
    body = await conn.api_post(
        target,
        operator,
        paths.WSA_DIRECTORY_SYNC_PATH,
        {"directory_id": directory_id},
        json={"ignoreSafeguards": False},
        content_type=_DIRECTORY_SYNC_TYPE,
    )
    return {
        "op_class": "write",
        "action": "sync",
        "directory_id": directory_id,
        "started": True,
        "answer": scalars(body, ("",)) if isinstance(body, dict) else {},
        "guidance": (
            "The sync runs in the background. Read idb.directory.list later to see the "
            "result of the sync."
        ),
    }


# ---------------------------------------------------------------------------
# SCIM users and groups (both products)
# ---------------------------------------------------------------------------


def scim_filter(attribute: str, name: str, match: str) -> str:
    """A SCIM filter ``<attribute> <match> "<name>"`` with quotes escaped."""
    if match not in MATCH_OPERATORS:
        raise ValueError(f"match must be one of {list(MATCH_OPERATORS)}, got {match!r}")
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'{attribute} {match} "{escaped}"'


def _search_body(params: dict[str, Any], attribute: str) -> dict[str, Any]:
    return {
        "filter": scim_filter(attribute, str(params["name"]), str(params.get("match", "co"))),
        "count": limit_of(params),
    }


def _extension(row: dict[str, Any]) -> dict[str, Any]:
    """The vendor extension of a SCIM resource (the ``urn:...`` object with ``domain``)."""
    for key, value in row.items():
        if str(key).startswith("urn:") and isinstance(value, dict) and "domain" in value:
            return value
    return {}


def _names(entries: object, cap: int) -> list[str]:
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = text(first(entry, "display", "value"))
            if name:
                out.append(name)
        if len(out) >= cap:
            break
    return out


def _primary_email(emails: object) -> str | None:
    rows = [e for e in emails if isinstance(e, dict)] if isinstance(emails, list) else []
    for row in rows:
        if flag(row.get("primary")):
            return text(row.get("value"))
    return text(rows[0].get("value")) if rows else None


def _user_row(row: dict[str, Any]) -> dict[str, Any]:
    ext = _extension(row)
    status = text(ext.get("userStatus"))
    groups = row.get("groups")
    return {
        "id": text(row.get("id")),
        "user_name": text(row.get("userName")),
        "display_name": text(row.get("displayName")),
        "email": _primary_email(row.get("emails")),
        "active": flag(row.get("active")),
        "domain": text(ext.get("domain")),
        "source": text(ext.get("internalUserType")),
        "user_status": _USER_STATUS.get(status or "", status),
        "groups": _names(groups, MAX_MEMBERS),
        "group_count": len(groups) if isinstance(groups, list) else 0,
    }


def _group_row(row: dict[str, Any]) -> dict[str, Any]:
    ext = _extension(row)
    members = row.get("members")
    count = len(members) if isinstance(members, list) else 0
    return {
        "id": text(row.get("id")),
        "display_name": text(row.get("displayName")),
        "domain": text(ext.get("domain")),
        "group_type": text(ext.get("internalGroupType")),
        "description": text(ext.get("description")),
        "member_count": count,
        "members": _names(members, MAX_MEMBERS),
        "members_truncated": count > MAX_MEMBERS,
    }


async def _scim_search(
    conn: IdbConnectorBase,
    operator: Operator,
    target: IdbTargetLike,
    template: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    # Identity Broker speaks application/scim+json; Workspace ONE Access 3.3 JSON.
    content_type = _SCIM_TYPE if template.startswith("/usergroup/") else "application/json"
    return await conn.api_post(target, operator, template, json=body, content_type=content_type)


async def scim_user_find(
    conn: IdbConnectorBase,
    operator: Operator,
    target: IdbTargetLike,
    params: dict[str, Any],
    template: str,
) -> dict[str, Any]:
    """SCIM ``Users/.search`` on ``userName``: one row per user."""
    answer = await _scim_search(conn, operator, target, template, _search_body(params, "userName"))
    rows = [_user_row(row) for row in items_of(answer, "Resources", "resources")]
    return listing("users", rows, limit_of(params), answer.get("totalResults"))


async def scim_group_find(
    conn: IdbConnectorBase,
    operator: Operator,
    target: IdbTargetLike,
    params: dict[str, Any],
    template: str,
) -> dict[str, Any]:
    """SCIM ``Groups/.search`` on ``displayName``: one row per group, with members."""
    answer = await _scim_search(
        conn, operator, target, template, _search_body(params, "displayName")
    )
    rows = [_group_row(row) for row in items_of(answer, "Resources", "resources")]
    return listing("groups", rows, limit_of(params), answer.get("totalResults"))

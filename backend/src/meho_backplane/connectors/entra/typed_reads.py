# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Entra ID typed read operations.

Each function here is the body behind a thin method on
:class:`~meho_backplane.connectors.entra.connector.EntraConnector`. The
operation table is in :mod:`meho_backplane.connectors.entra.typed_ops`, the
guidance for the AI in :mod:`meho_backplane.connectors.entra._llm_instructions`.

Every read asks Graph only for the fields its rows need (``$select``), and
every row is built from those fields. The app reads keep only the end dates of
client secrets and certificates (see :mod:`._credentials`).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.entra._credentials import (
    CredentialSummary,
    credential_fields,
    parse_graph_time,
    summarize,
    utc_text,
)
from meho_backplane.connectors.entra._errors import EntraApiError
from meho_backplane.connectors.entra._http import send
from meho_backplane.connectors.entra._paging import PagedRows, list_all_pages
from meho_backplane.connectors.entra._paths import (
    APPLICATIONS_PATH,
    APPLICATIONS_PERMISSION,
    GROUPS_PATH,
    GROUPS_PERMISSION,
    ROLE_ASSIGNMENTS_PATH,
    ROLE_DEFINITIONS_PATH,
    ROLE_PERMISSION,
    SERVICE_PRINCIPALS_PATH,
    USERS_PATH,
    USERS_PERMISSION,
    group_member_count_path,
)
from meho_backplane.connectors.entra.session import EntraTargetLike, is_guid

if TYPE_CHECKING:
    from meho_backplane.connectors.entra.connector import EntraConnector

__all__ = [
    "MEMBER_COUNT_MAX_GROUPS",
    "default_domain",
    "entra_about_impl",
    "entra_app_list_impl",
    "entra_group_list_impl",
    "entra_role_assignment_list_impl",
    "entra_service_principal_list_impl",
    "entra_user_list_impl",
]

#: MEHO counts group members only when the list has at most this many groups,
#: because each count is one more Graph call.
MEMBER_COUNT_MAX_GROUPS: Final[int] = 100

_USER_TYPES: Final[frozenset[str]] = frozenset({"Member", "Guest"})

#: The page sizes MEHO asks for: Graph's largest for each list.
_LARGE_PAGE: Final[str] = "999"
_SERVICE_PRINCIPAL_PAGE: Final[str] = "100"

_PRINCIPAL_TYPE_PREFIX: Final[str] = "#microsoft.graph."


def _utc_now() -> datetime:
    """The current time. Tests replace it with a fixed time."""
    return datetime.now(UTC)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _objects(rows: Iterable[object]) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict)]


# ----------------------------------------------------------------------
# entra.about
# ----------------------------------------------------------------------


def _domain_rows(organization: dict[str, Any]) -> list[dict[str, Any]]:
    domains = organization.get("verifiedDomains")
    return [
        {
            "name": _text(domain.get("name")),
            "is_default": _flag(domain.get("isDefault")),
            "is_initial": _flag(domain.get("isInitial")),
            "type": _text(domain.get("type")),
        }
        for domain in _objects(domains if isinstance(domains, list) else [])
    ]


def default_domain(organization: dict[str, Any]) -> str | None:
    """The name of the tenant's default verified domain, or ``None``."""
    for domain in _domain_rows(organization):
        if domain["is_default"] is True:
            return _text(domain["name"])
    return None


async def entra_about_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.about``: ``GET /v1.0/organization`` plus the login's ids and permissions.

    Returns the tenant ID, name and verified domains, the cloud, and the app
    ID, object ID and granted application permissions of the login. The ids
    and permissions are read from the access token's claims; the token itself
    is never returned.
    """
    del params  # the operation takes no parameters
    config, organization = await connector.read_organization(target, operator)
    login = await connector.login_identity(target, operator)
    return {
        "tenant_id": _text(organization.get("id")),
        "display_name": _text(organization.get("displayName")),
        "default_domain": default_domain(organization),
        "verified_domains": _domain_rows(organization),
        "cloud": config.cloud.name,
        "app_id": login.app_id,
        "object_id": login.object_id,
        "granted_permissions": list(login.roles) if login.roles is not None else None,
    }


# ----------------------------------------------------------------------
# entra.user.list
# ----------------------------------------------------------------------


def _user_filter(params: dict[str, Any]) -> str | None:
    """Build Graph's ``$filter`` from the checked parameters, or ``None``.

    Only fixed words go into the filter: ``Member`` or ``Guest``, and
    ``true`` or ``false``. No text from the caller is copied into it.
    """
    parts: list[str] = []
    user_type = params.get("user_type")
    if user_type is not None:
        if user_type not in _USER_TYPES:
            raise ValueError("entra.user.list: user_type must be Member or Guest.")
        parts.append(f"userType eq '{user_type}'")
    enabled = params.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("entra.user.list: enabled must be true or false.")
        parts.append(f"accountEnabled eq {'true' if enabled else 'false'}")
    return " and ".join(parts) or None


def _user_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(item.get("id")),
        "display_name": _text(item.get("displayName")),
        "user_principal_name": _text(item.get("userPrincipalName")),
        "account_enabled": _flag(item.get("accountEnabled")),
        "user_type": _text(item.get("userType")),
        "created_at": utc_text(parse_graph_time(item.get("createdDateTime"))),
    }


async def entra_user_list_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.user.list``: ``GET /v1.0/users``, optionally filtered by type and enabled."""
    query = {
        "$select": "id,displayName,userPrincipalName,accountEnabled,userType,createdDateTime",
        "$top": _LARGE_PAGE,
    }
    graph_filter = _user_filter(params)
    if graph_filter is not None:
        query["$filter"] = graph_filter
    paged = await list_all_pages(
        connector, target, USERS_PATH, operator=operator, permission=USERS_PERMISSION, params=query
    )
    paged.rows = [_user_row(item) for item in _objects(paged.rows)]
    return paged.as_dict()


# ----------------------------------------------------------------------
# entra.group.list
# ----------------------------------------------------------------------


def _group_row(item: dict[str, Any]) -> dict[str, Any]:
    raw_types = item.get("groupTypes")
    types = (
        {kind for kind in raw_types if isinstance(kind, str)}
        if isinstance(raw_types, list)
        else set()
    )
    mail = item.get("mailEnabled") is True
    security = item.get("securityEnabled") is True
    if "Unified" in types:
        group_type = "microsoft_365"
    elif security:
        group_type = "mail_enabled_security" if mail else "security"
    else:
        group_type = "distribution" if mail else "other"
    return {
        "id": _text(item.get("id")),
        "display_name": _text(item.get("displayName")),
        "group_type": group_type,
        "membership": "dynamic" if "DynamicMembership" in types else "assigned",
        "member_count": None,
    }


async def _member_count(
    connector: EntraConnector, operator: Operator, target: EntraTargetLike, group_id: object
) -> int | None:
    """Read the number of direct members of one group, or ``None``.

    ``None`` when the id is not a GUID (it never goes into a path then), when
    the group is gone (404), or when Graph's answer is not a number.
    """
    if not isinstance(group_id, str) or not is_guid(group_id):
        return None
    try:
        response = await send(
            connector,
            target,
            group_member_count_path(group_id),
            operator=operator,
            permission=GROUPS_PERMISSION,
            headers={"ConsistencyLevel": "eventual"},
        )
    except EntraApiError as exc:
        if exc.http_status == 404:
            return None
        raise
    # Graph sends the count as plain text, sometimes after a byte order mark.
    text = response.text.strip().lstrip("\ufeff")
    return int(text) if text.isascii() and text.isdigit() else None


async def entra_group_list_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.group.list``: ``GET /v1.0/groups``, with member counts on request.

    With ``include_member_count`` and at most :data:`MEMBER_COUNT_MAX_GROUPS`
    groups, each group's direct members are counted with
    ``GET /v1.0/groups/{id}/members/$count``. With more groups, MEHO counts
    none and says so in ``member_counts``.
    """
    paged = await list_all_pages(
        connector,
        target,
        GROUPS_PATH,
        operator=operator,
        permission=GROUPS_PERMISSION,
        params={
            "$select": "id,displayName,groupTypes,mailEnabled,securityEnabled",
            "$top": _LARGE_PAGE,
        },
    )
    rows = [_group_row(item) for item in _objects(paged.rows)]
    if not params.get("include_member_count"):
        status = "not_requested"
    elif paged.truncated or len(rows) > MEMBER_COUNT_MAX_GROUPS:
        status = "too_many_groups"
    else:
        for row in rows:
            row["member_count"] = await _member_count(connector, operator, target, row["id"])
        status = "counted"
    paged.rows = rows
    return {**paged.as_dict(), "member_counts": status}


# ----------------------------------------------------------------------
# entra.role.assignment.list
# ----------------------------------------------------------------------


def _principal_type(principal: object) -> str | None:
    """``user``, ``group`` or ``servicePrincipal`` from the principal's ``@odata.type``."""
    kind = principal.get("@odata.type") if isinstance(principal, dict) else None
    if not isinstance(kind, str) or not kind.startswith(_PRINCIPAL_TYPE_PREFIX):
        return None
    name = kind[len(_PRINCIPAL_TYPE_PREFIX) :]
    return name if name.isalpha() else None


def _role_assignment_row(item: dict[str, Any], role_names: dict[str, str]) -> dict[str, Any]:
    principal = item.get("principal")
    definition_id = _text(item.get("roleDefinitionId"))
    return {
        "id": _text(item.get("id")),
        "role_name": role_names.get(definition_id) if definition_id is not None else None,
        "role_definition_id": definition_id,
        "principal_id": _text(item.get("principalId")),
        "principal_type": _principal_type(principal),
        "principal_name": _text(principal.get("displayName"))
        if isinstance(principal, dict)
        else None,
        "scope": _text(item.get("directoryScopeId")),
    }


async def entra_role_assignment_list_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.role.assignment.list``: who holds which Entra ID admin role.

    Reads the role definitions once (for the role names), then
    ``GET /v1.0/roleManagement/directory/roleAssignments?$expand=principal``,
    which brings the type and name of each principal.
    """
    del params  # the operation takes no parameters
    definitions = await list_all_pages(
        connector, target, ROLE_DEFINITIONS_PATH, operator=operator, permission=ROLE_PERMISSION
    )
    role_names = {
        item["id"]: item["displayName"]
        for item in _objects(definitions.rows)
        if isinstance(item.get("id"), str) and isinstance(item.get("displayName"), str)
    }
    paged = await list_all_pages(
        connector,
        target,
        ROLE_ASSIGNMENTS_PATH,
        operator=operator,
        permission=ROLE_PERMISSION,
        params={"$expand": "principal"},
    )
    paged.rows = [_role_assignment_row(item, role_names) for item in _objects(paged.rows)]
    return paged.as_dict()


# ----------------------------------------------------------------------
# entra.app.list and entra.service_principal.list
# ----------------------------------------------------------------------


def _expiry_limit(params: dict[str, Any], now: datetime) -> datetime | None:
    """The latest end date the expiry filter keeps, or ``None`` for no filter."""
    days = params.get("expiring_within_days")
    if days is None:
        return None
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("expiring_within_days must be a whole number of days, 1 or more.")
    return now + timedelta(days=days)


def _runs_out_by(limit: datetime | None, *summaries: CredentialSummary) -> bool:
    """True when no filter is set, or a still-valid credential ends by *limit*."""
    if limit is None:
        return True
    return any(s.next_end is not None and s.next_end <= limit for s in summaries)


async def _credential_list(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
    *,
    path: str,
    query: dict[str, str],
    extra_fields: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    """List apps or service principals with their credential summaries.

    *extra_fields* names more columns as ``(row key, Graph field, kind)``,
    where kind is ``text`` or ``flag``.
    """
    now = _utc_now()
    limit = _expiry_limit(params, now)
    paged: PagedRows = await list_all_pages(
        connector, target, path, operator=operator, permission=APPLICATIONS_PERMISSION, params=query
    )
    items = _objects(paged.rows)
    rows: list[dict[str, Any]] = []
    for item in items:
        secrets = summarize(item.get("passwordCredentials"), now)
        certificates = summarize(item.get("keyCredentials"), now)
        if not _runs_out_by(limit, secrets, certificates):
            continue
        row: dict[str, Any] = {
            "id": _text(item.get("id")),
            "app_id": _text(item.get("appId")),
            "display_name": _text(item.get("displayName")),
        }
        for key, field, kind in extra_fields:
            row[key] = _flag(item.get(field)) if kind == "flag" else _text(item.get(field))
        row.update(credential_fields(secrets, certificates))
        rows.append(row)
    paged.rows = rows
    return {
        **paged.as_dict(),
        "read_count": len(items),
        "as_of": utc_text(now),
        "expiring_within_days": params.get("expiring_within_days"),
    }


async def entra_app_list_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.app.list``: ``GET /v1.0/applications`` with the end dates of their credentials."""
    return await _credential_list(
        connector,
        operator,
        target,
        params,
        path=APPLICATIONS_PATH,
        query={
            "$select": "id,appId,displayName,passwordCredentials,keyCredentials",
            "$top": _LARGE_PAGE,
        },
        extra_fields=(),
    )


async def entra_service_principal_list_impl(
    connector: EntraConnector,
    operator: Operator,
    target: EntraTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``entra.service_principal.list``: ``GET /v1.0/servicePrincipals`` with credential dates."""
    return await _credential_list(
        connector,
        operator,
        target,
        params,
        path=SERVICE_PRINCIPALS_PATH,
        query={
            "$select": (
                "id,appId,displayName,servicePrincipalType,accountEnabled,"
                "passwordCredentials,keyCredentials"
            ),
            "$top": _SERVICE_PRINCIPAL_PAGE,
        },
        extra_fields=(
            ("service_principal_type", "servicePrincipalType", "text"),
            ("account_enabled", "accountEnabled", "flag"),
        ),
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fleet role handlers for :class:`VcfOperationsConnector` (VCF Operations 9.1+).

In VCF 9.1, VCF Operations manages who may do what in the fleet. A person
or a group from the identity provider gets one or more VCF roles (for
example ``vcf_administrator``), each for the whole SSO realm or for some
VCF instances. These handlers read the roles, the SSO realms, the users
and groups of a realm, and change the roles of one user or group.

All calls are published in the VCF Operations 9.1 API
(``/suite-api/api/fleet-management/iam/...``). VCF Operations 9.0 has no
such calls, so every handler first checks the target's version and stops
on 9.0 with a clear message (:func:`require_fleet_iam_version`).

The vendor's ``PUT .../principals/{principalId}/roles`` replaces **all**
role assignments of a principal. :meth:`VropsFleetIamHandlers.fleet_role_assign`
therefore reads the current assignments first and sends the merged set;
when the merged set is empty it sends ``DELETE`` on the same path. It
refuses a change that would leave the realm without any user or group
holding the VCF Administrator role.

The handlers sit on a mixin so the connector module stays small (the same
pattern as :mod:`~meho_backplane.connectors.vcf_operations.licensing_handlers`).
The op metadata lives in
:mod:`~meho_backplane.connectors.vcf_operations.fleet_iam_ops`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
from packaging.version import Version

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.resolver import resolve_target_version

__all__ = [
    "FLEET_IAM_DECLARED_OP_IDS",
    "FLEET_IAM_MIN_VERSION",
    "LastFleetAdministratorError",
    "VropsFleetIamHandlers",
    "merge_role_assignments",
    "require_fleet_iam_version",
]

_IAM = "/suite-api/api/fleet-management/iam"
_ROLES_PATH = f"{_IAM}/roles"
_SSO_REALMS_PATH = f"{_IAM}/ssorealms"
_USERS_QUERY_PATH = f"{_IAM}/ssorealms/{{ssoRealmId}}/users/query"
_GROUPS_QUERY_PATH = f"{_IAM}/ssorealms/{{ssoRealmId}}/groups/query"
_PRINCIPAL_ROLES_PATH = f"{_IAM}/ssorealms/{{ssoRealmId}}/principals/{{principalId}}/roles"

#: Every vendor call these handlers make, as ``METHOD:/path``. The 9.1
#: spec-reconcile lane checks each one against the pinned API file.
FLEET_IAM_DECLARED_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        f"GET:{_ROLES_PATH}",
        f"GET:{_SSO_REALMS_PATH}",
        f"POST:{_USERS_QUERY_PATH}",
        f"POST:{_GROUPS_QUERY_PATH}",
        f"GET:{_PRINCIPAL_ROLES_PATH}",
        f"PUT:{_PRINCIPAL_ROLES_PATH}",
        f"DELETE:{_PRINCIPAL_ROLES_PATH}",
    }
)

#: The first VCF Operations release with the fleet-management IAM calls.
FLEET_IAM_MIN_VERSION: Final[str] = "9.1"
_MIN_VERSION: Final = Version(FLEET_IAM_MIN_VERSION)
#: The first dotted number in the version text. The fingerprint stores the
#: vendor's ``releaseName``, which can carry words around the number.
_VERSION_NUMBER: Final = re.compile(r"\d+(?:\.\d+)+")

_ROLE_PAGE_SIZE: Final[int] = 100
_ROLE_MAX_PAGES: Final[int] = 20
_PRINCIPAL_DEFAULT_PAGE_SIZE: Final[int] = 100
_ADMIN_SCAN_PAGE_SIZE: Final[int] = 200
_ADMIN_SCAN_MAX_PAGES: Final[int] = 25

#: How the built-in VCF Administrator role is named (role name and display
#: name, lower case). The user and group query answers name a role by
#: either form, so both count.
_ADMIN_ROLE_LABELS: Final[frozenset[str]] = frozenset({"vcf_administrator", "vcf administrator"})

_USER = "USER"
_GROUP = "GROUP"
_SSO_REALM = "SSO_REALM"
_VCF_INSTANCE = "VCF_INSTANCE"


class LastFleetAdministratorError(ValueError):
    """The change would leave the SSO realm without a VCF Administrator."""


def require_fleet_iam_version(target: Any, *, op_id: str) -> None:
    """Raise :exc:`NotImplementedError` when *target* runs VCF Operations older than 9.1.

    The dispatcher turns the error into ``connector_unsupported`` with the
    message in ``extras.detail``, before any vendor call. A target with no
    known version, or a version text without a number, goes on: the vendor's
    own answer is then reported as usual.
    """
    raw = resolve_target_version(target)
    if raw is None:
        return
    match = _VERSION_NUMBER.search(raw)
    if match is None or Version(match.group(0)) >= _MIN_VERSION:
        return
    raise NotImplementedError(
        f"{op_id}: the fleet roles API needs VCF Operations {FLEET_IAM_MIN_VERSION} "
        f"or newer; this target is {raw}."
    )


def _path(template: str, **ids: str) -> str:
    return template.format(**{key: quote(value, safe="") for key, value in ids.items()})


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _total_count(payload: Any) -> int | None:
    page_info = payload.get("pageInfo") if isinstance(payload, Mapping) else None
    total = page_info.get("totalCount") if isinstance(page_info, Mapping) else None
    return total if isinstance(total, int) else None


def _role_row(role: Mapping[str, Any]) -> dict[str, Any]:
    components = [
        f"{entry.get('componentType')}: {', '.join(str(r) for r in entry.get('roles') or [])}"
        for entry in role.get("componentRoles") or []
        if isinstance(entry, Mapping)
    ]
    return {
        "roleName": role.get("roleName"),
        "roleDisplayName": role.get("roleDisplayName"),
        "type": role.get("type"),
        "roleDescription": role.get("roleDescription"),
        "componentRoles": components,
    }


def _realm_row(realm: Mapping[str, Any]) -> dict[str, Any]:
    coverage = [
        f"{entry.get('ssoEntityType')}: {entry.get('name')}"
        for entry in realm.get("ssoRealmCoverage") or []
        if isinstance(entry, Mapping)
    ]
    issues = [
        f"{entry.get('severity')}: {entry.get('message') or entry.get('name')}"
        for entry in realm.get("issues") or []
        if isinstance(entry, Mapping)
    ]
    return {
        "id": realm.get("id"),
        "name": realm.get("name"),
        "vcfInstanceId": realm.get("vcfInstanceId"),
        "idpId": realm.get("idpId"),
        "totalConfiguredComponents": realm.get("totalConfiguredComponents"),
        "coverage": coverage,
        "issues": issues,
    }


def _user_row(user: Mapping[str, Any]) -> dict[str, Any]:
    roles = [r for r in user.get("roles") or [] if isinstance(r, Mapping)]
    names = " ".join(str(user.get(key)) for key in ("firstName", "lastName") if user.get(key))
    return {
        "principalType": _USER,
        "principalId": user.get("userId"),
        "name": user.get("userName"),
        "displayName": names or None,
        "domain": user.get("domainName"),
        "email": user.get("email"),
        "active": user.get("active"),
        "roles": [r.get("roleName") for r in roles],
        "directRoles": [
            r.get("roleName") for r in roles if "DIRECT" in (r.get("roleAssignmentTypes") or [])
        ],
    }


def _group_row(group: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "principalType": _GROUP,
        "principalId": group.get("groupId"),
        "name": group.get("groupName"),
        "displayName": group.get("displayName"),
        "domain": group.get("domainName"),
        "roles": [str(r) for r in group.get("roles") or []],
    }


def _assignment_row(assignment: Mapping[str, Any]) -> dict[str, Any]:
    scope = assignment.get("roleScope")
    scope = scope if isinstance(scope, Mapping) else {}
    return {
        "roleName": assignment.get("roleName"),
        "roleDisplayName": assignment.get("roleDisplayName"),
        "scopeType": scope.get("scopeType"),
        "resourceIds": [
            r.get("id") for r in scope.get("resources") or [] if isinstance(r, Mapping)
        ],
        "expiresAt": assignment.get("expiresAt"),
    }


def _wire_current(assignment: Mapping[str, Any]) -> dict[str, Any]:
    """An existing assignment as the PUT body carries it (display name dropped)."""
    wire: dict[str, Any] = {
        "roleName": assignment.get("roleName"),
        "roleScope": assignment.get("roleScope"),
    }
    if assignment.get("expiresAt") is not None:
        wire["expiresAt"] = assignment["expiresAt"]
    return wire


def _wire_new(entry: Mapping[str, Any]) -> dict[str, Any]:
    scope_type = entry.get("scopeType") or _SSO_REALM
    resource_ids = list(entry.get("resourceIds") or [])
    if scope_type == _VCF_INSTANCE and not resource_ids:
        raise ValueError(
            f"Role {entry['roleName']!r}: scopeType VCF_INSTANCE needs at least one id "
            "in resourceIds (the VCF instance ids from vrops.fleet.role.list)."
        )
    if scope_type == _SSO_REALM and resource_ids:
        raise ValueError(
            f"Role {entry['roleName']!r}: scopeType SSO_REALM covers the whole realm; "
            "leave resourceIds out."
        )
    wire: dict[str, Any] = {
        "roleName": entry["roleName"],
        "roleScope": {"scopeType": scope_type, "resources": [{"id": i} for i in resource_ids]},
    }
    if entry.get("expiresAt") is not None:
        wire["expiresAt"] = entry["expiresAt"]
    return wire


def _holds_admin_label(role_labels: Sequence[Any]) -> bool:
    return any(str(label).lower() in _ADMIN_ROLE_LABELS for label in role_labels)


def _holds_admin(assignments: Sequence[Mapping[str, Any]]) -> bool:
    return _holds_admin_label([a.get("roleName") for a in assignments])


def _require_known_roles(unknown: set[str]) -> None:
    if unknown:
        names = sorted(unknown)
        raise ConnectorResourceNotFoundError(
            names,
            f"Unknown VCF role: {', '.join(names)}. Use a roleName from vrops.fleet.role.list.",
        )


def _same_assignments(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> bool:
    """Do both lists hold the same assignments, in any order?"""

    def key(items: Sequence[Mapping[str, Any]]) -> list[str]:
        return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)

    return key(left) == key(right)


def merge_role_assignments(
    current: Sequence[Mapping[str, Any]],
    add: Sequence[Mapping[str, Any]],
    remove: Sequence[str],
) -> list[dict[str, Any]]:
    """Return the full assignment set to send after giving *add* and removing *remove*.

    The vendor PUT replaces every assignment, so the result keeps each
    current assignment that is not removed and not given again. A role in
    *add* that the principal already holds gets the new scope and expiry.
    """
    adding = {entry["roleName"] for entry in add}
    removing = set(remove)
    kept = [
        _wire_current(a)
        for a in current
        if a.get("roleName") not in removing and a.get("roleName") not in adding
    ]
    return kept + [_wire_new(entry) for entry in add]


if TYPE_CHECKING:
    # The concrete connector also inherits HttpConnector; naming it as the
    # base here only lets the type checker see the request seams.
    from meho_backplane.connectors.adapters.http import HttpConnector
    from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike

    _Base = HttpConnector
else:
    _Base = object


class VropsFleetIamHandlers(_Base):
    """The three fleet role handlers of the VCF Operations connector."""

    async def _fleet_roles(
        self, operator: Operator, target: VcfOperationsTargetLike, role_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Every VCF role, read page by page (the vendor needs page and pageSize)."""
        roles: list[dict[str, Any]] = []
        for page in range(_ROLE_MAX_PAGES):
            query: dict[str, Any] = {"page": page, "pageSize": _ROLE_PAGE_SIZE}
            if role_type:
                query["type"] = role_type
            payload = await self._request_json(
                target, "GET", _ROLES_PATH, operator=operator, params=query
            )
            batch = _rows(payload, "vcfRoles")
            roles.extend(batch)
            total = _total_count(payload)
            if len(batch) < _ROLE_PAGE_SIZE or (total is not None and len(roles) >= total):
                break
        return roles

    async def _sso_realms(
        self, operator: Operator, target: VcfOperationsTargetLike
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(target, "GET", _SSO_REALMS_PATH, operator=operator)
        return _rows(payload, "ssoRealms")

    async def _require_realm(
        self, operator: Operator, target: VcfOperationsTargetLike, realm_id: str
    ) -> None:
        realms = await self._sso_realms(operator, target)
        if any(str(r.get("id", "")).lower() == realm_id.lower() for r in realms):
            return
        known = ", ".join(f"{r.get('name')} ({r.get('id')})" for r in realms) or "none"
        raise ConnectorResourceNotFoundError(
            [realm_id],
            f"SSO realm {realm_id!r} not found. Known SSO realms: {known}. "
            "Read vrops.fleet.role.list for the realm ids.",
        )

    async def _principal_assignments(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        realm_id: str,
        principal_id: str,
    ) -> list[dict[str, Any]]:
        path = _path(_PRINCIPAL_ROLES_PATH, ssoRealmId=realm_id, principalId=principal_id)
        try:
            payload = await self._request_json(target, "GET", path, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            raise ConnectorResourceNotFoundError(
                [principal_id],
                f"No user or group {principal_id!r} in SSO realm {realm_id!r}. "
                "Find the id with vrops.fleet.principal.find.",
            ) from exc
        return _rows(payload, "vcfRoleAssignments")

    async def _query_principals(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        realm_id: str,
        principal_type: str,
        body: dict[str, Any],
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int | None]:
        template = _USERS_QUERY_PATH if principal_type == _USER else _GROUPS_QUERY_PATH
        payload = await self._post_json(
            target,
            _path(template, ssoRealmId=realm_id),
            operator=operator,
            params={"page": page, "pageSize": page_size},
            json=body,
        )
        if principal_type == _USER:
            return [_user_row(u) for u in _rows(payload, "vidbUsers")], _total_count(payload)
        return [_group_row(g) for g in _rows(payload, "vidbGroups")], _total_count(payload)

    async def _another_admin_exists(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        realm_id: str,
        principal_id: str,
    ) -> bool:
        """Does any other user or group in the realm hold the VCF Administrator role?"""
        for principal_type in (_USER, _GROUP):
            for page in range(_ADMIN_SCAN_MAX_PAGES):
                rows, total = await self._query_principals(
                    operator,
                    target,
                    realm_id,
                    principal_type,
                    {"filters": "HAS_ROLE"},
                    page,
                    _ADMIN_SCAN_PAGE_SIZE,
                )
                for row in rows:
                    if str(row.get("principalId")) == principal_id:
                        continue
                    if _holds_admin_label(row.get("roles") or []):
                        return True
                seen = (page + 1) * _ADMIN_SCAN_PAGE_SIZE
                if len(rows) < _ADMIN_SCAN_PAGE_SIZE or (total is not None and seen >= total):
                    break
        return False

    async def _refuse_last_admin(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        realm_id: str,
        principal_id: str,
    ) -> None:
        if await self._another_admin_exists(operator, target, realm_id, principal_id):
            return
        raise LastFleetAdministratorError(
            f"Refused: {principal_id!r} is the last user or group with the VCF "
            f"Administrator role in SSO realm {realm_id!r}. Give the role to another "
            "user or group first."
        )

    async def fleet_role_list(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.role.list`` — GET ``iam/roles`` (all pages) and GET ``iam/ssorealms``."""
        require_fleet_iam_version(target, op_id="vrops.fleet.role.list")
        roles = await self._fleet_roles(operator, target, params.get("type"))
        realms = await self._sso_realms(operator, target)
        return {
            "roles": [_role_row(r) for r in roles],
            "ssoRealms": [_realm_row(r) for r in realms],
        }

    async def fleet_principal_find(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.principal.find`` — search users or groups, or read one principal."""
        require_fleet_iam_version(target, op_id="vrops.fleet.principal.find")
        realm_id = params["ssoRealmId"]
        await self._require_realm(operator, target, realm_id)
        principal_id = params.get("principalId")
        if principal_id:
            assignments = await self._principal_assignments(
                operator, target, realm_id, principal_id
            )
            return {
                "ssoRealmId": realm_id,
                "principalId": principal_id,
                "assignments": [_assignment_row(a) for a in assignments],
            }
        principal_type = params["principalType"]
        body: dict[str, Any] = {}
        if params.get("roleFilter"):
            body["filters"] = params["roleFilter"]
        if params.get("name"):
            fields = (
                ("USERNAME", "FIRSTNAME", "LASTNAME")
                if principal_type == _USER
                else ("DISPLAY_NAME",)
            )
            body["searchTerms"] = {
                "anyOf": [
                    {"operator": "LIKE", "searchableField": field, "terms": [params["name"]]}
                    for field in fields
                ]
            }
        rows, total = await self._query_principals(
            operator,
            target,
            realm_id,
            principal_type,
            body,
            params.get("page", 0),
            params.get("pageSize", _PRINCIPAL_DEFAULT_PAGE_SIZE),
        )
        return {
            "ssoRealmId": realm_id,
            "principalType": principal_type,
            "totalCount": total,
            "principals": rows,
        }

    async def fleet_role_assign(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.role.assign`` — read, merge, then PUT (or DELETE) the principal's roles."""
        require_fleet_iam_version(target, op_id="vrops.fleet.role.assign")
        realm_id = params["ssoRealmId"]
        principal_id = params["principalId"]
        add: list[dict[str, Any]] = list(params.get("add") or [])
        remove: list[str] = list(params.get("remove") or [])
        both = {entry["roleName"] for entry in add} & set(remove)
        if both:
            raise ValueError(f"A role is in both add and remove: {', '.join(sorted(both))}.")

        await self._require_realm(operator, target, realm_id)
        known = {str(r.get("roleName")) for r in await self._fleet_roles(operator, target)}
        _require_known_roles({entry["roleName"] for entry in add} - known)
        current = await self._principal_assignments(operator, target, realm_id, principal_id)
        held = {str(a.get("roleName")) for a in current}
        _require_known_roles(set(remove) - known - held)

        after = merge_role_assignments(current, add, remove)
        result: dict[str, Any] = {
            "ssoRealmId": realm_id,
            "principalId": principal_id,
            "before": [_assignment_row(a) for a in current],
            "after": [_assignment_row(a) for a in after],
        }
        if _same_assignments(after, [_wire_current(a) for a in current]):
            return {**result, "changed": False, "vendorCall": None}
        if _holds_admin(current) and not _holds_admin(after):
            await self._refuse_last_admin(operator, target, realm_id, principal_id)

        path = _path(_PRINCIPAL_ROLES_PATH, ssoRealmId=realm_id, principalId=principal_id)
        if after:
            await self._post_json(
                target,
                path,
                operator=operator,
                verb="PUT",
                json={"vcfRoleAssignments": after},
            )
            vendor_call = "PUT"
        else:
            await self._post_json(target, path, operator=operator, verb="DELETE")
            vendor_call = "DELETE"
        return {**result, "changed": True, "vendorCall": vendor_call}

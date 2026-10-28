# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Microsoft Graph paths of the Entra ID connector.

Every path the connector calls is a ``*_PATH`` constant here. Each one starts
with the Graph version it uses: ``/v1.0/``. The connector never calls
``/beta/``, because Microsoft can change beta APIs at any time. A unit test
checks both rules (``backend/tests/test_connectors_entra.py``).

Microsoft publishes its own API description of Graph v1.0, an OpenAPI 3 file
(``openapi/v1.0/openapi.yaml`` in ``microsoftgraph/msgraph-metadata``). Every
path below is in that file. The file is about 44 MB, so it is not on the spec
shelf, and no CI check compares these paths with it yet.

Each path has a ``*_PERMISSION`` constant: the Microsoft Graph application
permission the call needs (the least one Microsoft names). When Graph refuses
a call, the error names this permission. ``Directory.Read.All`` also allows
every call here, but it allows much more.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "APPLICATIONS_PATH",
    "APPLICATIONS_PERMISSION",
    "GROUPS_PATH",
    "GROUPS_PERMISSION",
    "GROUP_MEMBER_COUNT_PATH",
    "ORGANIZATION_PATH",
    "ORGANIZATION_PERMISSION",
    "ROLE_ASSIGNMENTS_PATH",
    "ROLE_DEFINITIONS_PATH",
    "ROLE_PERMISSION",
    "SERVICE_PRINCIPALS_PATH",
    "USERS_PATH",
    "USERS_PERMISSION",
    "WIDE_PERMISSION",
    "group_member_count_path",
]

#: ``GET /v1.0/organization``: the tenant (List organizations). One object.
ORGANIZATION_PATH: Final[str] = "/v1.0/organization"

#: ``GET /v1.0/users``: the users (List users). Up to 999 per page.
USERS_PATH: Final[str] = "/v1.0/users"

#: ``GET /v1.0/groups``: the groups (List groups). Up to 999 per page.
GROUPS_PATH: Final[str] = "/v1.0/groups"

#: ``GET /v1.0/groups/{id}/members/$count``: the number of direct members of
#: one group (List group members, count only). Needs the header
#: ``ConsistencyLevel: eventual``. The answer is a plain number.
GROUP_MEMBER_COUNT_PATH: Final[str] = "/v1.0/groups/{group_id}/members/$count"

#: ``GET /v1.0/roleManagement/directory/roleAssignments``: who holds which
#: Entra ID admin role (List unifiedRoleAssignments, directory provider).
ROLE_ASSIGNMENTS_PATH: Final[str] = "/v1.0/roleManagement/directory/roleAssignments"

#: ``GET /v1.0/roleManagement/directory/roleDefinitions``: the Entra ID admin
#: roles, built-in and custom (List unifiedRoleDefinitions).
ROLE_DEFINITIONS_PATH: Final[str] = "/v1.0/roleManagement/directory/roleDefinitions"

#: ``GET /v1.0/applications``: the app registrations (List applications).
#: Up to 999 per page.
APPLICATIONS_PATH: Final[str] = "/v1.0/applications"

#: ``GET /v1.0/servicePrincipals``: the enterprise apps, also called service
#: principals (List servicePrincipals). Up to 100 per page.
SERVICE_PRINCIPALS_PATH: Final[str] = "/v1.0/servicePrincipals"

#: The application permission of ``entra.about``.
ORGANIZATION_PERMISSION: Final[str] = "Organization.Read.All"

#: The application permission of ``entra.user.list``.
USERS_PERMISSION: Final[str] = "User.Read.All"

#: The application permission of ``entra.group.list``, also for the member
#: count. ``GroupMember.Read.All`` works too.
GROUPS_PERMISSION: Final[str] = "Group.Read.All"

#: The application permission of ``entra.role.assignment.list``.
ROLE_PERMISSION: Final[str] = "RoleManagement.Read.Directory"

#: The application permission of ``entra.app.list`` and
#: ``entra.service_principal.list``.
APPLICATIONS_PERMISSION: Final[str] = "Application.Read.All"

#: The one application permission that allows every call above, and more.
WIDE_PERMISSION: Final[str] = "Directory.Read.All"


def group_member_count_path(group_id: str) -> str:
    """Return the member-count path of one group.

    *group_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.entra.session.is_guid`), so it cannot
    change the path.
    """
    return GROUP_MEMBER_COUNT_PATH.format(group_id=group_id)

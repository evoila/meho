# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Entra ID typed operations.

The operation table in :mod:`meho_backplane.connectors.entra.typed_ops` reads
them. Every parameter field has a description and every schema has at least
one example, as the operation contract check asks
(``backend/tests/test_operation_contracts.py``).
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "ABOUT_RESPONSE",
    "APP_LIST_PARAMS",
    "APP_LIST_RESPONSE",
    "EXPIRING_WITHIN_DAYS_MAX",
    "GROUP_LIST_PARAMS",
    "GROUP_LIST_RESPONSE",
    "NO_PARAMS",
    "ROLE_ASSIGNMENT_LIST_RESPONSE",
    "SERVICE_PRINCIPAL_LIST_PARAMS",
    "SERVICE_PRINCIPAL_LIST_RESPONSE",
    "USER_LIST_PARAMS",
    "USER_LIST_RESPONSE",
]

#: The JSON Schema ``type`` of a text field that may be null.
TEXT_OR_NULL: Final[list[str]] = ["string", "null"]

#: The most days ``expiring_within_days`` accepts: ten years.
EXPIRING_WITHIN_DAYS_MAX: Final[int] = 3650

#: The parameter schema of an operation that takes no parameters.
NO_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


#: The fields MEHO adds next to every list. They stay inline when the rows go
#: behind a stored-result link.
_PAGING_PROPERTIES: Final[dict[str, Any]] = {
    "truncated": {
        "type": "boolean",
        "description": "True when MEHO stopped reading pages at one of its limits.",
    },
    "omission": _text("What was left out and how to narrow the request, when truncated."),
    "omission_kind": _text("Which limit stopped the list: row_limit, page_limit or time_limit."),
    "pages_read": {"type": "integer", "description": "How many pages MEHO read from Graph."},
}

_PAGING_REQUIRED: Final[list[str]] = ["value", "truncated", "pages_read"]


def _list_response(row: dict[str, Any], what: str, **extra: Any) -> dict[str, Any]:
    """A list answer: ``value`` (the rows) plus the paging fields and *extra* fields."""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": row,
                "description": (
                    f"One row per {what}. A long list comes back as a stored-result link."
                ),
            },
            **_PAGING_PROPERTIES,
            **extra,
        },
        "required": [*_PAGING_REQUIRED, *extra],
    }


# ----------------------------------------------------------------------
# entra.about
# ----------------------------------------------------------------------

_DOMAIN_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _text("The domain name, for example example.com."),
        "is_default": {
            "type": ["boolean", "null"],
            "description": "True for the tenant's default domain.",
        },
        "is_initial": {
            "type": ["boolean", "null"],
            "description": "True for the first domain of the tenant (<name>.onmicrosoft.com).",
        },
        "type": _text("Managed (cloud sign-in) or Federated (sign-in through another service)."),
    },
}

ABOUT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "tenant_id": _text("The tenant (directory) ID, a GUID."),
        "display_name": _text("The tenant's name, as set in Microsoft Entra ID."),
        "default_domain": _text("The verified domain marked as the default one."),
        "verified_domains": {
            "type": "array",
            "items": _DOMAIN_ROW,
            "description": "The verified domains of the tenant.",
        },
        "cloud": {
            "type": "string",
            "description": "The cloud: AzureCloud, AzureUSGovernment or AzureChinaCloud.",
        },
        "app_id": _text("Application (client) ID of the login, read from the access token."),
        "object_id": _text("Object ID of the login's service principal, read from the token."),
        "granted_permissions": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "The Microsoft Graph application permissions the login's access token "
                "carries, for example User.Read.All. Null when the token cannot be read."
            ),
        },
    },
    "required": ["tenant_id", "cloud", "verified_domains"],
}

# ----------------------------------------------------------------------
# entra.user.list
# ----------------------------------------------------------------------

USER_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "user_type": {
            "type": "string",
            "enum": ["Member", "Guest"],
            "description": "Only users of this type: Member (own users) or Guest (invited).",
        },
        "enabled": {
            "type": "boolean",
            "description": "Only users whose account is enabled (true) or disabled (false).",
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"user_type": "Guest"}, {"enabled": False}],
}

_USER_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("The user's object ID, a GUID."),
        "display_name": _text("The user's name, as set in the directory."),
        "user_principal_name": _text("The sign-in name, for example a.person@example.com."),
        "account_enabled": {
            "type": ["boolean", "null"],
            "description": "True when the account may sign in.",
        },
        "user_type": _text("Member or Guest."),
        "created_at": _text("When the user was made (UTC). Null for some old users."),
    },
    "required": ["id"],
}

USER_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(_USER_ROW, "user")

# ----------------------------------------------------------------------
# entra.group.list
# ----------------------------------------------------------------------

GROUP_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "include_member_count": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also count the direct members of each group: one more Graph call per "
                "group, so MEHO counts only when the list has at most 100 groups."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"include_member_count": True}],
}

_GROUP_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("The group's object ID, a GUID."),
        "display_name": _text("The group's name, as set in the directory."),
        "group_type": {
            "type": "string",
            "enum": ["microsoft_365", "security", "mail_enabled_security", "distribution", "other"],
            "description": "Microsoft 365, security, mail-enabled security or distribution group.",
        },
        "membership": {
            "type": "string",
            "enum": ["assigned", "dynamic"],
            "description": "assigned (members added by hand) or dynamic (members by a rule).",
        },
        "member_count": {
            "type": ["integer", "null"],
            "description": "The number of direct members, or null when MEHO did not count.",
        },
    },
    "required": ["id", "group_type", "membership"],
}

GROUP_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _GROUP_ROW,
    "group",
    member_counts={
        "type": "string",
        "enum": ["counted", "not_requested", "too_many_groups"],
        "description": "Whether MEHO counted the members of the groups, and if not, why.",
    },
)

# ----------------------------------------------------------------------
# entra.role.assignment.list
# ----------------------------------------------------------------------

_ROLE_ASSIGNMENT_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("The ID of the role assignment."),
        "role_name": _text("The admin role, for example Global Administrator."),
        "role_definition_id": _text("The ID of the role definition."),
        "principal_id": _text("The object ID of the user, group or app that holds the role."),
        "principal_type": _text("user, group or servicePrincipal."),
        "principal_name": _text("The name of the principal, when the login may read it."),
        "scope": _text("Where the role applies: / for the whole tenant, else an object path."),
    },
    "required": ["id", "principal_id"],
}

ROLE_ASSIGNMENT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _ROLE_ASSIGNMENT_ROW, "role assignment"
)

# ----------------------------------------------------------------------
# entra.app.list and entra.service_principal.list
# ----------------------------------------------------------------------

_EXPIRING_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "expiring_within_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": EXPIRING_WITHIN_DAYS_MAX,
            "description": (
                "Only the apps with a client secret or certificate that is still valid and "
                "runs out within this many days. Leave it out to list every app."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"expiring_within_days": 30}],
}

APP_LIST_PARAMS: Final[dict[str, Any]] = _EXPIRING_PARAMS
SERVICE_PRINCIPAL_LIST_PARAMS: Final[dict[str, Any]] = _EXPIRING_PARAMS

_CREDENTIAL_PROPERTIES: Final[dict[str, Any]] = {
    "secret_count": {"type": "integer", "description": "How many client secrets the app has."},
    "expired_secret_count": {
        "type": "integer",
        "description": "How many of its client secrets have already run out.",
    },
    "next_secret_expiry": _text(
        "When the next still-valid client secret runs out (UTC), or null when none is valid."
    ),
    "certificate_count": {"type": "integer", "description": "How many certificates the app has."},
    "expired_certificate_count": {
        "type": "integer",
        "description": "How many of its certificates have already run out.",
    },
    "next_certificate_expiry": _text(
        "When the next still-valid certificate runs out (UTC), or null when none is valid."
    ),
}

_EXPIRY_LIST_PROPERTIES: Final[dict[str, Any]] = {
    "read_count": {
        "type": "integer",
        "description": "How many apps MEHO read from Graph, before the expiry filter.",
    },
    "as_of": {
        "type": "string",
        "description": "The time (UTC) MEHO compared the end dates with.",
    },
    "expiring_within_days": {
        "type": ["integer", "null"],
        "description": "The expiry filter that was used, or null for no filter.",
    },
}

_APP_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("The object ID of the app registration."),
        "app_id": _text("The application (client) ID."),
        "display_name": _text("The app's name, as set in the directory."),
        **_CREDENTIAL_PROPERTIES,
    },
    "required": ["id", "app_id", "secret_count", "certificate_count"],
}

APP_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _APP_ROW, "app registration", **_EXPIRY_LIST_PROPERTIES
)

_SERVICE_PRINCIPAL_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("The object ID of the enterprise app (service principal)."),
        "app_id": _text("The application (client) ID it belongs to."),
        "display_name": _text("The app's name, as set in the directory."),
        "service_principal_type": _text("Application, ManagedIdentity, Legacy or SocialIdp."),
        "account_enabled": {
            "type": ["boolean", "null"],
            "description": "True when the app may sign in.",
        },
        **_CREDENTIAL_PROPERTIES,
    },
    "required": ["id", "app_id", "secret_count", "certificate_count"],
}

SERVICE_PRINCIPAL_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _SERVICE_PRINCIPAL_ROW, "enterprise app", **_EXPIRY_LIST_PROPERTIES
)

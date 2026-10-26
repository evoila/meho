# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas and small schema helpers for the typed VCFA ops.

The read ops pass the vendor answer through, so their response schemas name
the vendor fields. The field names come from the public vendor SDKs:

* Provider plane (``/cloudapi/*``): the ``go-vcloud-director`` v3.0.0 types
  that the vendor's own Terraform provider uses for VCF Automation 9
  (``types/v56/tm.go`` ``TmOrg`` / ``Region``, ``types/v56/openapi.go``
  ``OpenApiPages`` / ``Right`` / ``Role`` / ``GlobalRole``).
* Tenant plane (``/iaas/api/*``): the ``vra-sdk-go`` v0.6.5 IaaS spec
  (``swagger/vra-iaas.json`` ``ProjectResult`` / ``Project`` /
  ``DeploymentResult`` / ``Deployment`` / ``About``), the same file the
  spec-reconcile lane pins.

The write ops return MEHO's own result shape, built by the handlers in
:mod:`._provisioning`, :mod:`._role_user` and :mod:`._api_token`; their
schemas name those keys. Response schemas are information for the AI: the
dispatcher does not check an answer against them.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ABOUT_RESPONSE",
    "IAAS_DEPLOYMENT_FIELDS",
    "IAAS_PROJECT_FIELDS",
    "ORG_FIELDS",
    "REGION_FIELDS",
    "RIGHT_FIELDS",
    "ROLE_FIELDS",
    "cloudapi_page",
    "field",
    "guidance",
    "iaas_page",
    "list_of",
    "obj",
]


def field(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description, **extra}


def obj(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


def guidance(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    preconditions: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The ``llm_instructions`` blob. ``preconditions`` is set on every change."""
    blob: dict[str, Any] = {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }
    if preconditions is not None:
        blob["preconditions"] = preconditions
    blob.update(extra)
    return blob


def _ref(what: str) -> dict[str, Any]:
    """A cloudapi reference: ``{name, id}``."""
    return obj(
        f"The {what}, as a name and id.",
        {
            "name": field("string", f"The {what} name."),
            "id": field("string", f"The {what} id (a URN)."),
        },
    )


# ---------------------------------------------------------------------------
# Provider plane (cloudapi)
# ---------------------------------------------------------------------------


def cloudapi_page(what: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """The cloudapi page answer: ``values`` plus the paging counts."""
    return obj(
        f"A page of {what}.",
        {
            "values": list_of(f"The {what} on this page.", item_properties),
            "resultTotal": field("integer", f"The total number of {what}."),
            "pageCount": field("integer", "The total number of pages."),
            "page": field("integer", "The current page number (starts at 1)."),
            "pageSize": field("integer", "The number of items per page."),
        },
    )


ORG_FIELDS: dict[str, Any] = {
    "id": field("string", "The org id (a URN)."),
    "name": field("string", "The org name, used in URLs."),
    "displayName": field("string", "The full display name."),
    "description": field("string", "The org description."),
    "isEnabled": field("boolean", "True when the org is enabled."),
    "isClassicTenant": field(
        "boolean", "True for a VM Apps org (the older, classic tenant style)."
    ),
    "orgVdcCount": field("integer", "The number of virtual data centers of the org."),
    "catalogCount": field("integer", "The number of catalogs of the org."),
    "userCount": field("integer", "The number of users of the org."),
    "managedBy": _ref("provider org that manages this org"),
}

REGION_FIELDS: dict[str, Any] = {
    "id": field("string", "The region id (a URN)."),
    "name": field("string", "The region name."),
    "description": field("string", "The region description."),
    "status": field(
        "string",
        "The region state: READY, NOT_READY, ERROR or FAILED. Only a READY region is usable.",
    ),
    "nsxManager": _ref("NSX Manager of the region"),
    "supervisors": list_of(
        "The vSphere Supervisors in the region.",
        {
            "name": field("string", "The Supervisor name."),
            "id": field("string", "The Supervisor id."),
        },
    ),
    "cpuCapacityMHz": field("integer", "The total CPU of the region, in MHz."),
    "memoryCapacityMiB": field("integer", "The total memory of the region, in MiB."),
    "storagePolicies": {
        "type": "array",
        "description": "The vCenter storage policy names added to the region.",
        "items": {"type": "string"},
    },
}

RIGHT_FIELDS: dict[str, Any] = {
    "id": field("string", "The right id (a URN)."),
    "name": field("string", "The exact right name, for example 'API Tokens: Manage'."),
    "description": field("string", "What the right allows."),
    "category": field("string", "The id of the right's category."),
    "rightType": field("string", "VIEW or MODIFY."),
    "bundleKey": field("string", "The key for the translated name."),
    "impliedRights": list_of(
        "Other rights this right includes.",
        {
            "name": field("string", "The right name."),
            "id": field("string", "The right id."),
        },
    ),
}

ROLE_FIELDS: dict[str, Any] = {
    "id": field("string", "The role id (a URN)."),
    "name": field("string", "The role name."),
    "description": field("string", "The role description."),
    "readOnly": field("boolean", "True for a built-in role that cannot be changed."),
    "bundleKey": field("string", "The key for the translated name."),
    "publishAll": field(
        ["boolean", "null"], "Global roles only: true when the role is published to every org."
    ),
}

# ---------------------------------------------------------------------------
# Tenant plane (IaaS)
# ---------------------------------------------------------------------------


def iaas_page(what: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """The IaaS page answer: ``content`` plus the counts."""
    return obj(
        f"A page of {what}.",
        {
            "content": list_of(f"The {what} on this page.", item_properties),
            "numberOfElements": field("integer", "The number of items on this page."),
            "totalElements": field("integer", f"The total number of {what}, when known."),
        },
    )


_PRINCIPAL_FIELDS: dict[str, Any] = {
    "email": field("string", "The user's email or the group name."),
    "type": field("string", "'user' or 'group'."),
}

IAAS_PROJECT_FIELDS: dict[str, Any] = {
    "id": field("string", "The project id."),
    "name": field("string", "The project name."),
    "description": field("string", "The project description."),
    "orgId": field("string", "The id of the org the project belongs to."),
    "administrators": list_of("The project administrators.", _PRINCIPAL_FIELDS),
    "members": list_of("The project members.", _PRINCIPAL_FIELDS),
    "viewers": list_of("The project viewers.", _PRINCIPAL_FIELDS),
    "zones": list_of(
        "The cloud zones the project may deploy to.",
        {
            "zoneId": field("string", "The cloud zone id."),
            "priority": field("integer", "The zone priority; a lower number wins."),
            "maxNumberInstances": field("integer", "The instance limit; 0 means no limit."),
        },
    ),
    "operationTimeout": field("integer", "The timeout for deploy tasks, in seconds."),
    "sharedResources": field("boolean", "True when the project's resources are shared."),
    "createdAt": field("string", "When the project was created (ISO 8601, UTC)."),
    "updatedAt": field("string", "When the project was last changed (ISO 8601, UTC)."),
}

IAAS_DEPLOYMENT_FIELDS: dict[str, Any] = {
    "id": field("string", "The deployment id."),
    "name": field("string", "The deployment name."),
    "description": field("string", "The deployment description."),
    "projectId": field("string", "The id of the project the deployment belongs to."),
    "orgId": field("string", "The id of the org the deployment belongs to."),
    "owner": field("string", "The user email or group name that owns the deployment."),
    "ownerType": field("string", "The owner kind: a user or a group."),
    "createdAt": field("string", "When the deployment was created (ISO 8601, UTC)."),
    "updatedAt": field("string", "When the deployment was last changed (ISO 8601, UTC)."),
}

ABOUT_RESPONSE: dict[str, Any] = obj(
    "The API versions the appliance supports.",
    {
        "latestApiVersion": field(
            "string", "The newest API version, as a date (yyyy-MM-dd), not a product version."
        ),
        "supportedApis": list_of(
            "Every supported API version.",
            {
                "apiVersion": field("string", "The API version, as a date (yyyy-MM-dd)."),
                "documentationLink": field("string", "A link to the docs of this version."),
                "deprecationPolicy": obj(
                    "Whether this version is deprecated, and until when it works.",
                    {
                        "deprecatedAt": field("string", "When the version was deprecated."),
                        "expiresAt": field("string", "When support for the version ends."),
                        "description": field("string", "Why it is deprecated and what to use."),
                    },
                ),
            },
        ),
    },
)

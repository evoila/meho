# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas for the ``vra8`` typed read core.

Every read passes the vendor answer through, so each schema names the vendor
fields. The names come from the public ``vra-sdk-go`` v0.6.5 Swagger files:
``swagger/vra-iaas.json`` (``ProjectResult``, ``Project``, ``About``),
``swagger/vra-catalog-deployment.json`` (``PageOfDeployment``,
``Deployment``, ``Request``, ``DeploymentResource``, ``PageOfCatalogItem``,
``CatalogItem``) and ``swagger/vra-blueprint.json`` (``PageOfBlueprint``,
``Blueprint``). Response schemas are information for the AI: the dispatcher
does not check an answer against them.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ABOUT_RESPONSE",
    "BLUEPRINT_PAGE_RESPONSE",
    "CATALOG_ITEM_PAGE_RESPONSE",
    "DEPLOYMENT_PAGE_RESPONSE",
    "DEPLOYMENT_RESPONSE",
    "PROJECT_PAGE_RESPONSE",
]


def _field(type_: str, description: str) -> dict[str, Any]:
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "string"}}


def _page(what: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """The Service Broker / Blueprint ``PageOf<X>`` answer."""
    return _object(
        f"A page of {what}.",
        {
            "content": _list_of(f"The {what} on this page.", item_properties),
            "totalElements": _field("integer", f"The total number of {what}."),
            "totalPages": _field("integer", "The total number of pages."),
            "numberOfElements": _field("integer", "The number of items on this page."),
            "number": _field("integer", "The page number, starting at 0."),
            "size": _field("integer", "The page size."),
            "first": _field("boolean", "True on the first page."),
            "last": _field("boolean", "True on the last page."),
        },
    )


_PRINCIPAL = {
    "email": _field("string", "The user's email or the group name."),
    "type": _field("string", "'user' or 'group'."),
}

PROJECT_PAGE_RESPONSE: dict[str, Any] = _object(
    "A page of projects (the IaaS ProjectResult).",
    {
        "content": _list_of(
            "The projects on this page.",
            {
                "id": _field("string", "The project id."),
                "name": _field("string", "The project name."),
                "description": _field("string", "The project description."),
                "orgId": _field("string", "The id of the org the project belongs to."),
                "administrators": _list_of("The project administrators.", _PRINCIPAL),
                "members": _list_of("The project members.", _PRINCIPAL),
                "viewers": _list_of("The project viewers.", _PRINCIPAL),
                "zones": _list_of(
                    "The cloud zones the project may deploy to, with their limits.",
                    {
                        "zoneId": _field("string", "The cloud zone id."),
                        "priority": _field("integer", "The zone priority; a lower number wins."),
                        "maxNumberInstances": _field(
                            "integer", "The instance limit; 0 means no limit."
                        ),
                        "cpuLimit": _field("integer", "The CPU limit; 0 means no limit."),
                        "memoryLimitMB": _field(
                            "integer", "The memory limit in MB; 0 means no limit."
                        ),
                    },
                ),
                "constraints": _object(
                    "Placement rules for storage, network and extensibility.", {}
                ),
                "customProperties": _object(
                    "Custom properties added to every request in the project.", {}
                ),
                "operationTimeout": _field("integer", "The timeout for deploy tasks, in seconds."),
                "machineNamingTemplate": _field("string", "The name template for new machines."),
                "sharedResources": _field("boolean", "True when the resources are shared."),
            },
        ),
        "numberOfElements": _field("integer", "The number of items on this page."),
        "totalElements": _field("integer", "The total number of projects, when known."),
    },
)

_REQUEST_FIELDS: dict[str, Any] = {
    "id": _field("string", "The request id."),
    "name": _field("string", "A short label of the request, for example the action."),
    "actionId": _field("string", "The id of the requested action."),
    "status": _field(
        "string",
        "The request state, for example PENDING, APPROVAL_PENDING, INPROGRESS, "
        "SUCCESSFUL, FAILED or ABORTED.",
    ),
    "requestedBy": _field("string", "Who asked for it."),
    "createdAt": _field("string", "When it was created."),
    "completedAt": _field("string", "When it finished."),
    "completedTasks": _field("integer", "How many tasks are done."),
    "totalTasks": _field("integer", "How many tasks there are."),
    "details": _field("string", "Longer details, for example the error."),
}

_DEPLOYMENT_FIELDS: dict[str, Any] = {
    "id": _field("string", "The deployment id."),
    "name": _field("string", "The deployment name."),
    "description": _field("string", "The deployment description."),
    "status": _field(
        "string",
        "The life-cycle state: CREATE_, UPDATE_ or DELETE_ followed by SUCCESSFUL, "
        "INPROGRESS or FAILED.",
    ),
    "projectId": _field("string", "The id of the project."),
    "orgId": _field("string", "The id of the org."),
    "blueprintId": _field("string", "The id of the blueprint it was made from."),
    "blueprintVersion": _field("string", "The blueprint version."),
    "catalogItemId": _field("string", "The id of the catalog item it was requested from."),
    "catalogItemVersion": _field("string", "The catalog item version."),
    "ownedBy": _field("string", "The owner."),
    "createdBy": _field("string", "Who created it."),
    "createdAt": _field("string", "When it was created."),
    "lastUpdatedAt": _field("string", "When it was last changed."),
    "leaseExpireAt": _field("string", "When its lease ends."),
    "inputs": _object("The inputs it was requested with.", {}),
}

DEPLOYMENT_PAGE_RESPONSE: dict[str, Any] = _page("deployments", _DEPLOYMENT_FIELDS)

DEPLOYMENT_RESPONSE: dict[str, Any] = _object(
    "One deployment, with its resources and request state.",
    {
        **_DEPLOYMENT_FIELDS,
        "resources": _list_of(
            "The resources of the deployment: machines, networks and more.",
            {
                "id": _field("string", "The resource id."),
                "name": _field("string", "The resource name."),
                "type": _field("string", "The resource type, for example Cloud.vSphere.Machine."),
                "state": _field("string", "The resource state: OK, PARTIAL or TAINTED."),
                "syncStatus": _field("string", "The sync state: SUCCESS, MISSING or STALE."),
                "properties": _object("The resource properties.", {}),
            },
        ),
        "lastRequest": _object("The most recent request on the deployment.", _REQUEST_FIELDS),
        "inprogressRequests": {
            "type": ["array", "object"],
            "description": "The requests running now; empty when nothing runs.",
        },
        "expense": _object(
            "The cost of the deployment.",
            {
                "totalExpense": _field("number", "The total cost."),
                "unit": _field("string", "The currency."),
            },
        ),
    },
)

BLUEPRINT_PAGE_RESPONSE: dict[str, Any] = _page(
    "blueprints",
    {
        "id": _field("string", "The blueprint id."),
        "name": _field("string", "The blueprint name."),
        "description": _field("string", "The blueprint description."),
        "projectId": _field("string", "The id of the project that owns it."),
        "projectName": _field("string", "The name of the project that owns it."),
        "status": _field("string", "DRAFT, VERSIONED or RELEASED."),
        "content": _field("string", "The blueprint YAML."),
        "valid": _field("boolean", "True when the last check found no error."),
        "totalVersions": _field("integer", "How many versions it has."),
        "totalReleasedVersions": _field("integer", "How many versions are released."),
        "requestScopeOrg": _field("boolean", "True when any project in the org may request it."),
        "contentSourceId": _field("string", "The id of the content source, such as a git repo."),
        "contentSourceType": _field("string", "The kind of content source, if any."),
        "contentSourcePath": _field("string", "The path in the content source."),
        "createdAt": _field("string", "When it was created."),
        "updatedAt": _field("string", "When it was last changed."),
    },
)

CATALOG_ITEM_PAGE_RESPONSE: dict[str, Any] = _page(
    "catalog items",
    {
        "id": _field("string", "The catalog item id."),
        "name": _field("string", "The catalog item name."),
        "description": _field("string", "The catalog item description."),
        "type": _object(
            "The source kind, for example a blueprint or a workflow.",
            {
                "id": _field("string", "The type id."),
                "name": _field("string", "The type name."),
            },
        ),
        "sourceId": _field("string", "The id of the content source."),
        "sourceName": _field("string", "The name of the content source."),
        "projectIds": _strings("The ids of the projects that may request it."),
        "bulkRequestLimit": _field("integer", "How many may be requested at once."),
        "createdAt": _field("string", "When it was created."),
        "lastUpdatedAt": _field("string", "When it was last changed."),
    },
)

ABOUT_RESPONSE: dict[str, Any] = _object(
    "The IaaS API versions the appliance supports.",
    {
        "latestApiVersion": _field(
            "string", "The newest API version, as a date (yyyy-MM-dd), not a product version."
        ),
        "supportedApis": _list_of(
            "Every supported API version.",
            {
                "apiVersion": _field("string", "The API version, as a date (yyyy-MM-dd)."),
                "documentationLink": _field("string", "A link to the docs of this version."),
                "deprecationPolicy": _object(
                    "Whether this version is deprecated, and until when it works.",
                    {
                        "deprecatedAt": _field("string", "When the version was deprecated."),
                        "expiresAt": _field("string", "When support for the version ends."),
                        "description": _field("string", "Why it is deprecated and what to use."),
                    },
                ),
            },
        ),
    },
)

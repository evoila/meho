# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas of the vROps (VCF Operations) typed read operations.

Each schema names the main fields an operation returns, so the AI knows the
shape before it calls. The schemas stay open (``additionalProperties: true``):
they describe the answer, they do not check it. The handlers pass the vendor
payload through as it is.

Field names come from the pinned VCF Operations 9.0 Suite API file
(``vcf-operations-openapi.ingestable.json``, ``components.schemas``:
``version``, ``alerts`` / ``alert``, ``resources`` / ``resource``,
``stats-of-resources`` / ``stats``, ``resource-kind-attributes`` /
``resource-kind-attribute``). The 8.x connector shares these operations,
except the resource kind properties read, which is 9.x only; no 8.x Suite
API file is on the spec shelf.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VROPS_ALERT_LIST_RESPONSE",
    "VROPS_LIVENESS_RESPONSE",
    "VROPS_RESOURCE_KIND_PROPERTIES_RESPONSE",
    "VROPS_RESOURCE_QUERY_RESPONSE",
    "VROPS_RESOURCE_STATS_RESPONSE",
]


def _field(type_: str | list[str], description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


_PAGE_INFO = _object(
    "Paging data of the answer.",
    {
        "totalCount": _field("integer", "The total number of results."),
        "page": _field("integer", "The current page number (0-based)."),
        "pageSize": _field("integer", "The number of entries per page."),
    },
)

_LINKS = _list_of(
    "Links to related pages.",
    {
        "href": _field("string", "The link URL."),
        "rel": _field("string", "How the link relates, for example NEXT or SELF."),
        "name": _field("string", "The link name."),
    },
)

VROPS_LIVENESS_RESPONSE: dict[str, Any] = _object(
    "The appliance version (vendor version object).",
    {
        "releaseName": _field(["string", "null"], "The release name, e.g. VCF Operations 9.0.0.0."),
        "buildNumber": _field(["integer", "string", "null"], "The build number."),
        "major": _field("integer", "The major API version."),
        "minor": _field("integer", "The minor API version."),
        "minorMinor": _field("integer", "The bug-fix API version."),
        "releasedDate": _field("integer", "The release date, in epoch milliseconds."),
        "humanlyReadableReleaseDate": _field("string", "The release date as text."),
        "description": _field("string", "A description of the release."),
    },
)

VROPS_ALERT_LIST_RESPONSE: dict[str, Any] = _object(
    "A page of alerts (vendor alerts object).",
    {
        "alerts": _list_of(
            "One row per alert.",
            {
                "alertId": _field("string", "The alert UUID."),
                "resourceId": _field("string", "The UUID of the resource that raised the alert."),
                "alertDefinitionId": _field("string", "The id of the alert definition."),
                "alertDefinitionName": _field("string", "The name of the alert definition."),
                "alertLevel": _field(
                    "string",
                    "The criticality: UNKNOWN, NONE, INFORMATION, WARNING, IMMEDIATE, "
                    "CRITICAL or AUTO.",
                ),
                "alertImpact": _field("string", "The impact of the alert."),
                "type": _field("string", "The alert type."),
                "subType": _field("string", "The alert sub-type."),
                "status": _field(
                    "string", "The alert condition: NEW, ACTIVE, UPDATED or CANCELED."
                ),
                "controlState": _field(
                    "string", "The control state: OPEN, ASSIGNED, SUSPENDED or SUPPRESSED."
                ),
                "startTimeUTC": _field("integer", "When the alert started (epoch time)."),
                "cancelTimeUTC": _field("integer", "When the alert was cancelled."),
                "updateTimeUTC": _field("integer", "When the alert was last updated."),
                "statKey": _field("string", "The stat key the alert is about, if any."),
                "ownerName": _field("string", "The user who owns the alert."),
            },
        ),
        "pageInfo": _PAGE_INFO,
        "links": _LINKS,
    },
)

VROPS_RESOURCE_QUERY_RESPONSE: dict[str, Any] = _object(
    "A page of resources (vendor resources object).",
    {
        "resourceList": _list_of(
            "One row per matching resource.",
            {
                "identifier": _field("string", "The resource UUID."),
                "resourceKey": _object(
                    "What the resource is.",
                    {
                        "name": _field("string", "The resource name."),
                        "adapterKindKey": _field("string", "The adapter kind, e.g. VMWARE."),
                        "resourceKindKey": _field(
                            "string", "The resource kind, e.g. VirtualMachine."
                        ),
                        "resourceIdentifiers": {
                            "type": "array",
                            "description": "The identifiers the adapter uses for the resource.",
                        },
                    },
                ),
                "resourceStatusStates": _list_of(
                    "The collection status and state per adapter instance.",
                    {
                        "adapterInstanceId": _field("string", "The adapter instance id."),
                        "resourceStatus": _field(
                            "string", "The data collection status, e.g. DATA_RECEIVING."
                        ),
                        "resourceState": _field("string", "The collection state, e.g. STARTED."),
                        "statusMessage": _field("string", "A status message."),
                    },
                ),
                "resourceHealth": _field(
                    "string", "The health: GREEN, YELLOW, ORANGE, RED or GREY."
                ),
                "resourceHealthValue": _field("number", "The health score."),
                "creationTime": _field("integer", "When vROps first saw the resource."),
                "badges": _list_of(
                    "Badge scores, such as HEALTH, RISK or CAPACITY_REMAINING.",
                    {
                        "type": _field("string", "The badge type."),
                        "color": _field("string", "The badge color."),
                        "score": _field("number", "The badge score."),
                    },
                ),
            },
        ),
        "pageInfo": _PAGE_INFO,
        "links": _LINKS,
    },
)

VROPS_RESOURCE_STATS_RESPONSE: dict[str, Any] = _object(
    "The latest stat values per resource (vendor stats-of-resources object).",
    {
        "values": _list_of(
            "One row per resource.",
            {
                "resourceId": _field("string", "The resource UUID."),
                "stat-list": _object(
                    "The stats of this resource.",
                    {
                        "stat": _list_of(
                            "One row per stat key.",
                            {
                                "statKey": _object(
                                    "The stat this row is about.",
                                    {"key": _field("string", "The stat key, e.g. cpu|demandmhz.")},
                                ),
                                "timestamps": {
                                    "type": "array",
                                    "description": "The sample times, in epoch milliseconds.",
                                },
                                "data": {
                                    "type": "array",
                                    "description": "The numeric values, one per timestamp.",
                                },
                                "values": {
                                    "type": "array",
                                    "description": "The text values, for text stats.",
                                },
                                "rollUpType": _field("string", "How samples are rolled up."),
                            },
                        ),
                    },
                ),
            },
        ),
    },
)

VROPS_RESOURCE_KIND_PROPERTIES_RESPONSE: dict[str, Any] = _object(
    "The properties one resource kind has (vendor resource-kind-attributes object).",
    {
        "resourceTypeAttributes": _list_of(
            "One row per property.",
            {
                "key": _field("string", "The property key, e.g. config|guestFullName."),
                "name": _field("string", "The display name of the property."),
                "description": _field("string", "What the property holds."),
                "dataType2": _field(
                    "string",
                    "The data type: STRING, INTEGER, HOST, IP, ENUM, PASSWORD, BOOLEAN, "
                    "FLOAT or UNKNOWN.",
                ),
                "dataType": _field("string", "The old data type field (deprecated)."),
                "unit": _field("string", "The unit of the value, if any."),
                "property": _field("boolean", "True when the row is a property."),
                "defaultMonitored": _field(
                    "boolean", "True when the property is collected by default."
                ),
            },
        ),
    },
)

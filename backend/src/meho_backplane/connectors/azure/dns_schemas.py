# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure public DNS operations (#5368).

The operation table is in :mod:`meho_backplane.connectors.azure.dns`. The
name patterns here are also the checks the operation bodies run before a name
goes into a URL path (:mod:`.dns_parse`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL
from meho_backplane.connectors.azure.network_schemas import (
    RESOURCE_GROUP_CHARS,
    RESOURCE_GROUP_MAX_LENGTH,
)

__all__ = [
    "READ_RECORD_TYPES",
    "RECORD_NAME_CHARS",
    "RECORD_NAME_MAX_LENGTH",
    "RECORD_SET_DELETE_PARAMS",
    "RECORD_SET_DELETE_RESPONSE",
    "RECORD_SET_LIST_PARAMS",
    "RECORD_SET_LIST_RESPONSE",
    "RECORD_SET_SHOW_PARAMS",
    "RECORD_SET_SHOW_RESPONSE",
    "RECORD_SET_UPSERT_PARAMS",
    "RECORD_SET_UPSERT_RESPONSE",
    "WRITE_RECORD_TYPES",
    "ZONE_CHARS",
    "ZONE_LIST_RESPONSE",
    "ZONE_MAX_LENGTH",
]

#: Every record type Azure DNS has, for the reads and for a delete. SOA is
#: only listed: Azure deletes it with the zone.
READ_RECORD_TYPES: Final[tuple[str, ...]] = (
    "A",
    "AAAA",
    "CAA",
    "CNAME",
    "MX",
    "NS",
    "PTR",
    "SOA",
    "SRV",
    "TXT",
)

#: The types MEHO creates or changes. NS and SOA are left out: they tell the
#: world which servers answer for the zone, and Azure manages them itself.
WRITE_RECORD_TYPES: Final[tuple[str, ...]] = (
    "A",
    "AAAA",
    "CAA",
    "CNAME",
    "MX",
    "PTR",
    "SRV",
    "TXT",
)

#: A zone name without its final dot: labels of letters, digits, ``_`` and
#: ``-``, joined by dots. It starts and ends with a letter, a digit or ``_``,
#: so ``.`` and ``..`` never pass and the URL path cannot move.
ZONE_CHARS: Final[str] = r"\w(?:[\w.-]{0,251}\w)?"
ZONE_MAX_LENGTH: Final[int] = 253

#: A record set name relative to the zone: ``@`` (the zone itself), or labels
#: that may hold ``*`` (a wildcard) and ``_`` (for example ``_dmarc``).
RECORD_NAME_CHARS: Final[str] = r"@|[\w*](?:[\w.*-]{0,251}[\w*-])?"
RECORD_NAME_MAX_LENGTH: Final[int] = 253

_ZONE_PATTERN: Final[str] = f"^{ZONE_CHARS}$(?!\\n)"
_GROUP_PATTERN: Final[str] = f"^{RESOURCE_GROUP_CHARS}$(?!\\n)"
_NAME_PATTERN: Final[str] = f"^(?:{RECORD_NAME_CHARS})$(?!\\n)"

_EXAMPLE_GROUP: Final[str] = "rg-dns-example"
_EXAMPLE_ZONE: Final[str] = "example.com"
_EXAMPLE_ETAG: Final[str] = "00000000-0000-0000-0000-000000000000"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _int(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _group_param() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": RESOURCE_GROUP_MAX_LENGTH,
        "pattern": _GROUP_PATTERN,
        "description": "The resource group of the DNS zone, as Azure shows it.",
    }


def _zone_param() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": ZONE_MAX_LENGTH,
        "pattern": _ZONE_PATTERN,
        "description": "The DNS zone name without a final dot, for example example.com.",
    }


def _name_param() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": RECORD_NAME_MAX_LENGTH,
        "pattern": _NAME_PATTERN,
        "description": (
            "The record set name relative to the zone: www for www.example.com, "
            "@ for example.com itself, * for a wildcard."
        ),
    }


def _type_param(types: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": list(types),
        "description": "The DNS record type of the record set.",
    }


def _etag_param(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "not": {"enum": ["*"]},
        "description": description,
    }


# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------

_ZONE_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _text("The zone name, for example example.com."),
        "resource_group": _text("The resource group, read from the Azure resource ID."),
        "location": _text("Always global for a DNS zone."),
        "id": _text("The Azure resource ID of the zone."),
        "etag": _text("The version of the zone, from Azure."),
        "zone_type": _text("Public or Private. This operation lists public zones."),
        "record_set_count": _int("How many record sets (DNS names with a type) the zone holds."),
        "max_record_sets": _int("How many record sets the zone may hold."),
        "name_servers": _texts("The Azure name servers that answer for the zone."),
    },
    "required": ["name"],
}

_RECORD_SET_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _text("The record set name relative to the zone (@ is the zone itself)."),
        "type": _text("The record type: A, AAAA, CAA, CNAME, MX, NS, PTR, SOA, SRV or TXT."),
        "fqdn": _text("The full DNS name, with a final dot."),
        "ttl": _int("The time to live in seconds."),
        "etag": _text("The version of the record set. Pass it to change or delete this version."),
        "values": _texts(
            "The record values as one text each: an address, a name, "
            "'preference exchange' (MX), 'priority weight port target' (SRV), "
            "'flags tag value' (CAA) or the text (TXT)."
        ),
        "alias_target_id": _text(
            "The Azure resource the name points to, when the record set is an alias "
            "(then values is empty), else null."
        ),
        "provisioning_state": _text("Succeeded or another state from Azure."),
        "id": _text("The Azure resource ID of the record set."),
    },
    "required": ["name", "type", "values"],
}


def _paged(row: dict[str, Any], what: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": row,
                "description": (
                    f"One row per {what}. More than 50 rows (or more than 4 KB) come back "
                    "as a link to the stored result; read it with result_query."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when MEHO stopped reading pages at one of its limits.",
            },
            "omission": _text("What was left out and how to narrow the request, when truncated."),
            "omission_kind": _text(
                "Which limit stopped the list: row_limit, page_limit or time_limit."
            ),
            "pages_read": {
                "type": "integer",
                "description": "How many pages MEHO read from Azure.",
            },
        },
        "required": ["value", "truncated", "pages_read"],
    }


ZONE_LIST_RESPONSE: Final[dict[str, Any]] = _paged(_ZONE_ROW, "public DNS zone")
RECORD_SET_LIST_RESPONSE: Final[dict[str, Any]] = _paged(_RECORD_SET_ROW, "record set")

_DETAIL_FIELDS: Final[dict[str, Any]] = {
    **_RECORD_SET_ROW["properties"],
    "records": {
        "type": "array",
        "items": {"type": ["object", "string"]},
        "description": (
            "The records in Azure's own fields: strings for A, AAAA, CNAME, PTR and TXT, "
            "objects for MX (preference, exchange), SRV (priority, weight, port, target), "
            "CAA (flags, tag, value), NS (nsdname) and SOA."
        ),
    },
    "metadata": {
        "type": ["object", "null"],
        "description": "The metadata (name and value texts) kept on the record set.",
    },
}

RECORD_SET_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {**_DETAIL_FIELDS, "zone": {"type": "string", "description": "The zone name."}},
    "required": ["name", "type", "values", "records"],
}

RECORD_SET_DELETE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "deleted": {"type": "boolean", "description": "True when Azure deleted the record set."},
        "already_absent": {
            "type": "boolean",
            "description": "True when Azure had no such record set (nothing was deleted).",
        },
        "zone": {"type": "string", "description": "The zone name."},
        "resource_group": {"type": "string", "description": "The resource group."},
        "previous": {
            "type": ["object", "null"],
            "description": (
                "The record set as it was just before the delete: name, type, fqdn, ttl, "
                "etag, values, records. This is the copy to recreate it from."
            ),
        },
    },
    "required": ["deleted", "already_absent", "previous"],
}

RECORD_SET_UPSERT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "description": "created or updated."},
        "zone": {"type": "string", "description": "The zone name."},
        "resource_group": {"type": "string", "description": "The resource group."},
        "record_set": {
            "type": "object",
            "description": "The record set as Azure answered after the write.",
            "properties": _DETAIL_FIELDS,
        },
    },
    "required": ["state", "record_set"],
}

# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

RECORD_SET_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"resource_group": _group_param(), "zone": _zone_param()},
    "required": ["resource_group", "zone"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP, "zone": _EXAMPLE_ZONE}],
}

RECORD_SET_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _group_param(),
        "zone": _zone_param(),
        "record_type": _type_param(READ_RECORD_TYPES),
        "name": _name_param(),
    },
    "required": ["resource_group", "zone", "record_type", "name"],
    "additionalProperties": False,
    "examples": [
        {
            "resource_group": _EXAMPLE_GROUP,
            "zone": _EXAMPLE_ZONE,
            "record_type": "A",
            "name": "www",
        }
    ],
}

RECORD_SET_DELETE_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _group_param(),
        "zone": _zone_param(),
        "record_type": _type_param(tuple(t for t in READ_RECORD_TYPES if t != "SOA")),
        "name": _name_param(),
        "etag": _etag_param(
            "The etag of the record set that the person approved: copy it from "
            "azure.network.dns.recordset.show. Azure refuses the delete when the record "
            "set changed since."
        ),
    },
    "required": ["resource_group", "zone", "record_type", "name", "etag"],
    "additionalProperties": False,
    "examples": [
        {
            "resource_group": _EXAMPLE_GROUP,
            "zone": _EXAMPLE_ZONE,
            "record_type": "A",
            "name": "old-host",
            "etag": _EXAMPLE_ETAG,
        }
    ],
}


def _record_field(kind: str, description: str, maximum: int | None = None) -> dict[str, Any]:
    if kind == "text":
        return {"type": "string", "minLength": 1, "maxLength": 4096, "description": description}
    return {"type": "integer", "minimum": 0, "maximum": maximum, "description": description}


_RECORD_ITEM: Final[dict[str, Any]] = {
    "oneOf": [
        {"type": "string", "minLength": 1, "maxLength": 4096},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "preference": _record_field("number", "MX: lower is tried first.", 65535),
                "exchange": _record_field("text", "MX: the mail server name."),
                "priority": _record_field("number", "SRV: lower is tried first.", 65535),
                "weight": _record_field("number", "SRV: share among equal priorities.", 65535),
                "port": _record_field("number", "SRV: the port of the service.", 65535),
                "target": _record_field("text", "SRV: the host name that runs the service."),
                "flags": _record_field("number", "CAA: the flags, 0 to 255.", 255),
                "tag": _record_field("text", "CAA: issue, issuewild or iodef."),
                "value": _record_field("text", "CAA: the value of the tag."),
            },
        },
    ]
}

RECORD_SET_UPSERT_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _group_param(),
        "zone": _zone_param(),
        "record_type": _type_param(WRITE_RECORD_TYPES),
        "name": _name_param(),
        "ttl": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2147483647,
            "description": "The time to live in seconds, for example 3600.",
        },
        "records": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": _RECORD_ITEM,
            "description": (
                "The records of the set. A and AAAA: one address text each. CNAME: exactly "
                "one name text. PTR: one name text each. TXT: one text each (at most 255 "
                "characters). MX: objects {preference, exchange}. SRV: objects {priority, "
                "weight, port, target}. CAA: objects {flags, tag, value}."
            ),
        },
        "etag": _etag_param(
            "Leave out to create a new record set (Azure refuses when the name already "
            "exists). To change an existing one, pass its etag from "
            "azure.network.dns.recordset.show: Azure refuses when it changed since."
        ),
    },
    "required": ["resource_group", "zone", "record_type", "name", "ttl", "records"],
    "additionalProperties": False,
    "examples": [
        {
            "resource_group": _EXAMPLE_GROUP,
            "zone": _EXAMPLE_ZONE,
            "record_type": "A",
            "name": "www",
            "ttl": 3600,
            "records": ["203.0.113.10"],
        }
    ],
}

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Input checks of the Azure public DNS operations (#5368).

:func:`record_set_path` checks the four names a call gives and builds the path
of one record set; :func:`record_set_body` checks the records of a write and
builds the ``PUT`` body. Both raise ``ValueError`` with a text that never
repeats a value the caller gave.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from meho_backplane.connectors.azure._paths import DNS_RECORD_SET_PATH
from meho_backplane.connectors.azure.dns_schemas import (
    READ_RECORD_TYPES,
    RECORD_NAME_CHARS,
    RECORD_NAME_MAX_LENGTH,
    WRITE_RECORD_TYPES,
    ZONE_CHARS,
    ZONE_MAX_LENGTH,
)
from meho_backplane.connectors.azure.network_schemas import (
    RESOURCE_GROUP_CHARS,
    RESOURCE_GROUP_MAX_LENGTH,
)

__all__ = [
    "checked_group_and_zone",
    "checked_name",
    "record_set_body",
    "record_set_path",
    "record_type_of",
]

_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_CHARS)
_ZONE: Final[re.Pattern[str]] = re.compile(ZONE_CHARS)
_NAME: Final[re.Pattern[str]] = re.compile(RECORD_NAME_CHARS)

#: The most text of one TXT string. Longer text must be split in Azure's own
#: strings of 255 characters; MEHO sends one string for each record.
_TXT_STRING_MAX: Final[int] = 255


def checked_name(
    params: Mapping[str, Any], key: str, pattern: re.Pattern[str], max_length: int
) -> str:
    """Return ``params[key]`` when it is a text of the right shape, else raise ``ValueError``."""
    value = params.get(key)
    if not isinstance(value, str) or len(value) > max_length or not pattern.fullmatch(value):
        raise ValueError(
            f"{key} is not a valid Azure name. Pass the name exactly as Azure shows it."
        )
    return value


def checked_group_and_zone(params: Mapping[str, Any]) -> tuple[str, str]:
    """The resource group and the zone of a call, checked."""
    return (
        checked_name(params, "resource_group", _GROUP, RESOURCE_GROUP_MAX_LENGTH),
        checked_name(params, "zone", _ZONE, ZONE_MAX_LENGTH),
    )


def record_type_of(params: Mapping[str, Any], allowed: tuple[str, ...] = READ_RECORD_TYPES) -> str:
    """The record type of a call, upper case, when it is one of *allowed*."""
    value = params.get("record_type")
    if not isinstance(value, str) or value.upper() not in allowed:
        raise ValueError(f"record_type must be one of: {', '.join(allowed)}.")
    return value.upper()


def record_set_path(
    subscription_id: str,
    params: Mapping[str, Any],
    *,
    allowed_types: tuple[str, ...] = READ_RECORD_TYPES,
) -> tuple[str, str, str, str, str]:
    """Check the names of a call. Return the path and the checked names.

    The tuple is ``(path, resource_group, zone, record_type, name)``. The
    names are checked and percent-encoded in the path, so none of them can
    change what the path points to.
    """
    group, zone = checked_group_and_zone(params)
    record_type = record_type_of(params, allowed_types)
    name = checked_name(params, "name", _NAME, RECORD_NAME_MAX_LENGTH)
    path = DNS_RECORD_SET_PATH.format(
        subscription_id=subscription_id,
        resource_group=quote(group, safe=""),
        zone=quote(zone, safe=""),
        record_type=record_type,
        name=quote(name, safe="@"),
    )
    return path, group, zone, record_type, name


# ----------------------------------------------------------------------
# The records of a write
# ----------------------------------------------------------------------


def _refuse(what: str) -> ValueError:
    return ValueError(f"records: {what}")


def _address(record: object, version: int) -> dict[str, str]:
    kind = "an IPv4" if version == 4 else "an IPv6"
    if not isinstance(record, str):
        raise _refuse(f"each record must be {kind} address text.")
    try:
        parsed = ipaddress.ip_address(record)
    except ValueError:
        raise _refuse(f"each record must be {kind} address.") from None
    if parsed.version != version:
        raise _refuse(f"each record must be {kind} address.")
    return {("ipv4Address" if version == 4 else "ipv6Address"): str(parsed)}


def _name_text(record: object, field: str) -> dict[str, str]:
    if not isinstance(record, str) or not record.strip():
        raise _refuse("each record must be a name text.")
    return {field: record.strip()}


def _txt(record: object) -> dict[str, list[str]]:
    if not isinstance(record, str) or not record:
        raise _refuse("each TXT record must be a non-empty text.")
    if len(record) > _TXT_STRING_MAX:
        raise _refuse(f"a TXT record is at most {_TXT_STRING_MAX} characters.")
    return {"value": [record]}


def _object(record: object, fields: tuple[str, ...], kind: str) -> dict[str, Any]:
    """An object with exactly *fields*; a whole number for every field except text ones."""
    if not isinstance(record, dict) or set(record) != set(fields):
        raise _refuse(f"each {kind} record must be an object with {', '.join(fields)}.")
    out: dict[str, Any] = {}
    for field in fields:
        value = record[field]
        is_text = field in {"exchange", "target", "tag", "value"}
        if is_text != isinstance(value, str) or isinstance(value, bool) or value in ("", None):
            raise _refuse(f"{kind} field {field} has the wrong type.")
        out[field] = value.strip() if isinstance(value, str) else value
    return out


_PROPERTY: Final[dict[str, str]] = {
    "A": "ARecords",
    "AAAA": "AAAARecords",
    "CAA": "caaRecords",
    "MX": "MXRecords",
    "PTR": "PTRRecords",
    "SRV": "SRVRecords",
    "TXT": "TXTRecords",
}


def _one(record_type: str, record: object) -> dict[str, Any]:
    if record_type == "A":
        return _address(record, 4)
    if record_type == "AAAA":
        return _address(record, 6)
    if record_type == "PTR":
        return _name_text(record, "ptrdname")
    if record_type == "TXT":
        return _txt(record)
    if record_type == "MX":
        return _object(record, ("preference", "exchange"), "MX")
    if record_type == "SRV":
        return _object(record, ("priority", "weight", "port", "target"), "SRV")
    return _object(record, ("flags", "tag", "value"), "CAA")


def record_set_body(params: Mapping[str, Any], record_type: str) -> dict[str, Any]:
    """Build the ``PUT`` body from ``ttl`` and ``records``. Raises ``ValueError`` when wrong."""
    if record_type not in WRITE_RECORD_TYPES:
        raise ValueError(f"record_type must be one of: {', '.join(WRITE_RECORD_TYPES)}.")
    ttl = params.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 2147483647:
        raise ValueError("ttl must be a whole number of seconds from 1 to 2147483647.")
    records = params.get("records")
    if not isinstance(records, list) or not records:
        raise _refuse("pass at least one record.")
    if record_type == "CNAME":
        if len(records) != 1:
            raise _refuse("a CNAME record set holds exactly one record.")
        properties: dict[str, Any] = {"CNAMERecord": _name_text(records[0], "cname")}
    else:
        properties = {_PROPERTY[record_type]: [_one(record_type, record) for record in records]}
    return {"properties": {"TTL": ttl, **properties}}

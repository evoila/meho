# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How the Azure DNS operations build their rows from Azure's answers (#5368).

Every row is built from a fixed list of fields; nothing else from Azure's
answer is passed on. The functions here only read the JSON Azure sent. The
calls are in :mod:`meho_backplane.connectors.azure.dns_reads`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

__all__ = ["record_set_detail", "record_set_row", "zone_row"]

Row = dict[str, Any]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _resource_group_of(resource_id: str | None) -> str | None:
    if resource_id is None:
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[index + 1] or None
    return None


def zone_row(item: Mapping[str, Any]) -> Row:
    """One public DNS zone: identity, record set counts and name servers."""
    props = _mapping(item.get("properties"))
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "resource_group": _resource_group_of(resource_id),
        "location": _text(item.get("location")),
        "id": resource_id,
        "etag": _text(item.get("etag")),
        "zone_type": _text(props.get("zoneType")) or "Public",
        "record_set_count": _int(props.get("numberOfRecordSets")),
        "max_record_sets": _int(props.get("maxNumberOfRecordSets")),
        "name_servers": _texts(props.get("nameServers")),
    }


# ----------------------------------------------------------------------
# Record values
# ----------------------------------------------------------------------


def _txt(record: Mapping[str, Any]) -> str | None:
    return "".join(_texts(record.get("value"))) or None


def _joined(*fields: str) -> Callable[[Mapping[str, Any]], str | None]:
    def build(record: Mapping[str, Any]) -> str | None:
        parts = [record.get(field) for field in fields]
        if any(part is None or isinstance(part, bool) for part in parts):
            return None
        return " ".join(str(part) for part in parts)

    return build


def _single(field: str) -> Callable[[Mapping[str, Any]], str | None]:
    return lambda record: _text(record.get(field))


#: Per record type: the property that holds the records, and how one record
#: becomes one text. CNAME holds one object, not a list (handled apart).
_LISTS: Final[dict[str, tuple[str, Callable[[Mapping[str, Any]], str | None]]]] = {
    "A": ("ARecords", _single("ipv4Address")),
    "AAAA": ("AAAARecords", _single("ipv6Address")),
    "CAA": ("caaRecords", _joined("flags", "tag", "value")),
    "MX": ("MXRecords", _joined("preference", "exchange")),
    "NS": ("NSRecords", _single("nsdname")),
    "PTR": ("PTRRecords", _single("ptrdname")),
    "SRV": ("SRVRecords", _joined("priority", "weight", "port", "target")),
    "TXT": ("TXTRecords", _txt),
}

_SOA: Final[tuple[str, ...]] = (
    "host",
    "email",
    "serialNumber",
    "refreshTime",
    "retryTime",
    "expireTime",
    "minimumTTL",
)


def _type_of(item: Mapping[str, Any]) -> str | None:
    """``Microsoft.Network/dnsZones/A`` becomes ``A``."""
    raw = _text(item.get("type"))
    return raw.rsplit("/", 1)[-1].upper() if raw else None


def _records(record_type: str | None, props: Mapping[str, Any]) -> list[Any]:
    """The records in Azure's own fields (strings for the simple types)."""
    if record_type == "CNAME":
        cname = _text(_mapping(props.get("CNAMERecord")).get("cname"))
        return [cname] if cname else []
    if record_type == "SOA":
        soa = _mapping(props.get("SOARecord"))
        return [{key: soa.get(key) for key in _SOA}] if soa else []
    if record_type not in _LISTS:
        return []
    key, _ = _LISTS[record_type]
    records = _items(props.get(key))
    if record_type in {"A", "AAAA", "PTR"}:
        field = {"A": "ipv4Address", "AAAA": "ipv6Address", "PTR": "ptrdname"}[record_type]
        return [text for text in (_text(record.get(field)) for record in records) if text]
    if record_type == "TXT":
        return [text for text in (_txt(record) for record in records) if text is not None]
    return [dict(record) for record in records]


def _values(record_type: str | None, props: Mapping[str, Any]) -> list[str]:
    """One text for each record."""
    if record_type == "CNAME":
        return [str(value) for value in _records("CNAME", props)]
    if record_type == "SOA":
        soa = _mapping(props.get("SOARecord"))
        return [" ".join(str(soa.get(key)) for key in _SOA)] if soa else []
    if record_type not in _LISTS:
        return []
    key, build = _LISTS[record_type]
    values = (build(record) for record in _items(props.get(key)))
    return [value for value in values if value is not None]


def record_set_row(item: Mapping[str, Any]) -> Row:
    """One record set: name, type, TTL, version and the values as texts."""
    props = _mapping(item.get("properties"))
    record_type = _type_of(item)
    return {
        "name": _text(item.get("name")),
        "type": record_type,
        "fqdn": _text(props.get("fqdn")),
        "ttl": _int(props.get("TTL")),
        "etag": _text(item.get("etag")),
        "values": _values(record_type, props),
        "alias_target_id": _text(_mapping(props.get("targetResource")).get("id")),
        "provisioning_state": _text(props.get("provisioningState")),
        "id": _text(item.get("id")),
    }


def record_set_detail(item: Mapping[str, Any]) -> Row:
    """One record set with its records in Azure's own fields and its metadata."""
    props = _mapping(item.get("properties"))
    metadata = props.get("metadata")
    return {
        **record_set_row(item),
        "records": _records(_type_of(item), props),
        "metadata": dict(metadata) if isinstance(metadata, dict) else None,
    }

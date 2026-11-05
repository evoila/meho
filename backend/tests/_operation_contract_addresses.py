# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rule 13 of the operation contract check: an address field carries the marker.

A parameter that holds an address MEHO connects to (or that the target
connects to for MEHO) carries ``x-meho-caller-address: true`` in its schema.
MEHO then runs the operation at risk level ``caution`` or higher and waits
for a person's approval (``meho_backplane.operations.caller_address``).

This module finds the fields that look like an address but have no marker.
A field looks like an address when:

- its name is ``url``, ``uri``, ``host``, ``hostname``, ``address``,
  ``server`` or ``resolver``, or ends in ``_url``, ``_host`` or
  ``_address``; or
- its schema ``format`` is ``uri``, ``url``, ``hostname``, ``ipv4`` or
  ``ipv6``.

A marker on the field itself or on a field around it counts (a list of URLs
may carry the marker on the list). A field that only looks like an address
goes in :data:`NOT_CALLER_ADDRESSES` with its reason: MEHO and the target
never connect to it, MEHO connects to it only on the target's own host
(the Azure poll URL), or it is the address of a built-in network check
(``net.*``), which the probe allowlist guards instead of approval. Used by
``test_operation_contracts.py``; tested with fake operations in
``test_operation_contract_rules.py``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER

ADDRESS_NAMES: Final[frozenset[str]] = frozenset(
    {"url", "uri", "host", "hostname", "address", "server", "resolver"}
)
ADDRESS_NAME_ENDINGS: Final[tuple[str, ...]] = ("_url", "_host", "_address")
ADDRESS_FORMATS: Final[frozenset[str]] = frozenset({"uri", "url", "hostname", "ipv4", "ipv6"})

#: Fields that look like an address, but are not a caller address:
#: ``(operation id, field path) -> reason``. The live check fails when an
#: entry no longer names a registered field that looks like an address.
_EVIDENCE_LINK: Final[str] = "a link stored as evidence on the topology; MEHO never opens it"
BUILT_IN_CHECK_REASON: Final[str] = (
    "a built-in network check; the probe allowlist (MEHO_NETDIAG_PROBE_ALLOWLIST) "
    "bounds where it connects, so it needs no approval and a sensor can run it"
)
NOT_CALLER_ADDRESSES: Final[Mapping[tuple[str, str], str]] = {
    ("azure.operation.status", "operation.poll_url"): (
        "the poll URL an earlier Azure write returned; MEHO polls it only when it is "
        "on the target's own Azure host (check_poll_url)"
    ),
    ("freeipa.hbac.test", "host"): (
        "a host name that FreeIPA checks against its access rules; nothing connects to it"
    ),
    ("phpipam.address.reserve", "hostname"): (
        "the host name stored with the reserved address in phpIPAM; nothing connects to it"
    ),
    ("targets.register", "host"): (
        "the host of a new target, stored in the registry; registering connects to nothing, "
        "and later calls run that target's own operations"
    ),
    ("topology.annotate", "evidence_url"): _EVIDENCE_LINK,
    ("winsrv.storage.iscsi.connect", "node_address"): (
        "the iSCSI name (IQN) of the target, not a network address; the host connects "
        "to the portal in target_portal_address, which carries the marker"
    ),
    ("net.dns_lookup", "resolver"): BUILT_IN_CHECK_REASON,
    ("net.http_probe", "url"): BUILT_IN_CHECK_REASON,
    ("net.ntp_check", "host"): BUILT_IN_CHECK_REASON,
    ("net.path_mtu", "host"): BUILT_IN_CHECK_REASON,
    ("net.ping", "host"): BUILT_IN_CHECK_REASON,
    ("net.ssh_keyscan", "host"): BUILT_IN_CHECK_REASON,
    ("net.tcp_check", "host"): BUILT_IN_CHECK_REASON,
    ("net.tls_inspect", "host"): BUILT_IN_CHECK_REASON,
    ("net.trace", "host"): BUILT_IN_CHECK_REASON,
    ("topology.bulk_import", "rows[].evidence_url"): _EVIDENCE_LINK,
    ("topology.create_node", "evidence_url"): _EVIDENCE_LINK,
}

_NESTED_KEYS: Final[tuple[str, ...]] = ("items", "additionalProperties")
_LIST_KEYS: Final[tuple[str, ...]] = ("anyOf", "oneOf", "allOf", "prefixItems")
_MAP_KEYS: Final[tuple[str, ...]] = ("$defs", "definitions")


def _name_looks_like_address(name: str) -> bool:
    lowered = name.lower()
    return lowered in ADDRESS_NAMES or lowered.endswith(ADDRESS_NAME_ENDINGS)


def _is_marked(schema: Mapping[str, Any]) -> bool:
    return schema.get(CALLER_ADDRESS_MARKER) is True


def _address_fields(
    schema: object, path: str, *, name: str | None, marked: bool
) -> Iterator[tuple[str, bool]]:
    """Yield ``(field path, marked)`` for each field of *schema* that looks like an address."""
    if not isinstance(schema, Mapping):
        return
    marked = marked or _is_marked(schema)
    named = name is not None and _name_looks_like_address(name)
    if named or schema.get("format") in ADDRESS_FORMATS:
        yield path, marked
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for prop, sub in properties.items():
            sub_path = f"{path}.{prop}" if path else str(prop)
            yield from _address_fields(sub, sub_path, name=str(prop), marked=marked)
    for key in _NESTED_KEYS:
        yield from _address_fields(schema.get(key), f"{path}[]", name=None, marked=marked)
    for key in _LIST_KEYS:
        for sub in schema.get(key) or ():
            yield from _address_fields(sub, path, name=name, marked=marked)
    for key in _MAP_KEYS:
        defs = schema.get(key)
        if isinstance(defs, Mapping):
            for def_name, sub in defs.items():
                yield from _address_fields(sub, f"{key}/{def_name}", name=None, marked=False)


def address_fields(parameter_schema: Mapping[str, Any]) -> dict[str, bool]:
    """Every field of *parameter_schema* that looks like an address -> whether it is marked."""
    found: dict[str, bool] = {}
    for path, marked in _address_fields(parameter_schema, "", name=None, marked=False):
        found[path] = found.get(path, True) and marked
    return found


def unmarked_address_fields(op_id: str, parameter_schema: Mapping[str, Any]) -> list[str]:
    """The address-shaped fields of *op_id* with no marker and not in the allow list."""
    return [
        path
        for path, marked in address_fields(parameter_schema).items()
        if not marked and (op_id, path) not in NOT_CALLER_ADDRESSES
    ]

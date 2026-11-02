# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for the host settings read and the NTP, DNS and syslog changes.

* An always-on test pins the vim calls each operation may make.
* The shelf tests check, in the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 update files, that every vim method is a served POST path, that
  every property the read takes is served, that ``HostConfigManager`` has
  every manager the operations use, and that every field the request bodies
  send exists on its type.

The shelf tests skip when the spec shelf is not there and run in CI. Follows
``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _host
from meho_backplane.connectors.vmware_rest.composites._host_config_ops import (
    HOST_CONFIG_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.host_config import (
    CONFIG_MANAGER_TYPES,
    OP_QUERY_OPTIONS,
    OP_RETRIEVE_PROPERTIES,
    PROPERTY_BY_TYPE,
)
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = [
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
]

#: Every field each request body (and the read) uses, per vim type.
_BODY_FIELDS: dict[str, set[str]] = {
    "HostDateTimeConfig": {"ntpConfig"},
    "HostNtpConfig": {"server"},
    "HostDnsConfig": {"dhcp", "hostName", "domainName", "address", "searchDomain"},
    "OptionValue": {"key", "value"},
    "UpdateDateTimeConfigRequestType": {"config"},
    "UpdateDnsConfigRequestType": {"config"},
    "UpdateServicePolicyRequestType": {"id", "policy"},
    "StartServiceRequestType": {"id"},
    "RestartServiceRequestType": {"id"},
    "UpdateOptionsRequestType": {"changedValue"},
    "QueryOptionsRequestType": {"name"},
    "HostDateTimeInfo": {"timeZone", "systemClockProtocol", "ntpConfig"},
    "HostService": {"key", "label", "running", "policy"},
    "HostSnmpConfigSpec": {"enabled", "port", "readOnlyCommunities", "trapTargets"},
    "HostSnmpDestination": {"hostName", "port"},
    "HostDiagnosticPartition": {"storageType", "slots", "id"},
    "HostScsiDiskPartition": {"diskName", "partition"},
}


def _all_vim_calls() -> set[str]:
    calls = {OP_RETRIEVE_PROPERTIES, OP_QUERY_OPTIONS}
    for sub_ops in HOST_CONFIG_GOVERNED_SUBOPS.values():
        calls |= set(sub_ops)
    return calls | set(_host._VIM_SUB_OPS_HOST_SERVICE_CONTROL)


def test_the_vim_calls_are_pinned() -> None:
    """Shape gate (always runs)."""
    assert HOST_CONFIG_GOVERNED_SUBOPS == {
        "vmware.composite.host.ntp.set": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig",
            "POST:/HostServiceSystem/{moId}/UpdateServicePolicy",
            "POST:/HostServiceSystem/{moId}/StartService",
            "POST:/HostServiceSystem/{moId}/RestartService",
        ),
        "vmware.composite.host.dns.set": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/HostNetworkSystem/{moId}/UpdateDnsConfig",
        ),
        "vmware.composite.host.syslog.set": (
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "POST:/OptionManager/{moId}/QueryOptions",
            "POST:/OptionManager/{moId}/UpdateOptions",
        ),
    }
    assert PROPERTY_BY_TYPE == {
        "HostDateTimeSystem": "dateTimeInfo",
        "HostServiceSystem": "serviceInfo",
        "HostNetworkSystem": "dnsConfig",
        "HostAccessManager": "lockdownMode",
        "HostSnmpSystem": "configuration",
        "HostDiagnosticSystem": "activePartition",
    }


def _schema_fields(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema (a line scan, like the sibling lanes)."""
    fields: set[str] = set()
    inside = False
    in_props = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     ") and line.strip():
            break
        if line.startswith("      ") and not line.startswith("       "):
            in_props = line.strip() == "properties:"
            continue
        if in_props and line.startswith("        ") and not line.startswith("         "):
            key = line.strip()
            if key.endswith(":"):
                fields.add(key[:-1])
    return fields


def _post_paths(lines: list[str]) -> set[str]:
    """Path items that have a ``post`` operation."""
    paths: set[str] = set()
    current: str | None = None
    for line in lines:
        if line.startswith("  /") and line.endswith(":") and not line.startswith("   "):
            current = line.strip()[:-1]
        elif current is not None and line == "    post:":
            paths.add(current)
    return paths


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_vim_call_is_served(shelf: str, filename: str) -> None:
    lines = require_shelf_spec(shelf, filename).read_text(encoding="utf-8").splitlines()
    served = _post_paths(lines)
    missing = sorted(op.partition(":")[2] for op in _all_vim_calls())
    missing = [path for path in missing if path not in served]
    assert not missing, f"{shelf}/{filename} does not serve POST {missing}"


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_property_and_manager_exists(shelf: str, filename: str) -> None:
    lines = require_shelf_spec(shelf, filename).read_text(encoding="utf-8").splitlines()
    for mo_type, prop in PROPERTY_BY_TYPE.items():
        assert f"  /{mo_type}/{{moId}}/{prop}:" in lines, (shelf, filename, mo_type, prop)
    managers = _schema_fields(lines, "HostConfigManager")
    assert set(CONFIG_MANAGER_TYPES) <= managers, sorted(set(CONFIG_MANAGER_TYPES) - managers)


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_body_field_exists(shelf: str, filename: str) -> None:
    lines = require_shelf_spec(shelf, filename).read_text(encoding="utf-8").splitlines()
    missing = {
        vim_type: sorted(fields - _schema_fields(lines, vim_type))
        for vim_type, fields in _BODY_FIELDS.items()
    }
    missing = {vim_type: names for vim_type, names in missing.items() if names}
    assert not missing, f"{shelf}/{filename} lacks these VIM fields: {missing}"

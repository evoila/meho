# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for the host security reads and changes.

* An always-on test pins the vim calls each change may make.
* The shelf tests check, in the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 update files, that every vim method is a served POST path, that
  every property the operations read is served, that ``HostConfigManager``
  has every manager they use, and that every field the request bodies send
  (and the parsers read) exists on its type.

The shelf tests skip when the spec shelf is not there and run in CI. Follows
``docs/decisions/spec-reconcile-guards-standard.md``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites._host_security_ops import (
    HOST_SECURITY_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.host_security import OP_QUERY_OPTIONS
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = [
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
]

#: Managed object type -> the properties the operations read from it.
_PROPERTIES: dict[str, set[str]] = {
    "OptionManager": {"supportedOption"},
    "HostFirewallSystem": {"firewallInfo"},
    "HostAccessManager": {"lockdownMode"},
    "HostSnmpSystem": {"configuration"},
    "HostDiagnosticSystem": {"activePartition"},
}
_MANAGERS = {
    "advancedOption",
    "firewallSystem",
    "hostAccessManager",
    "snmpSystem",
    "diagnosticSystem",
}

#: Every field each request body sends, and each parser reads, per vim type.
_FIELDS: dict[str, set[str]] = {
    "QueryOptionsRequestType": {"name"},
    "UpdateOptionsRequestType": {"changedValue"},
    "OptionValue": {"key", "value"},
    "OptionDef": {"optionType"},
    "OptionType": {"valueIsReadonly"},
    "BoolOption": {"defaultValue"},
    "IntOption": {"min", "max", "defaultValue"},
    "LongOption": {"min", "max", "defaultValue"},
    "FloatOption": {"min", "max", "defaultValue"},
    "StringOption": {"defaultValue"},
    "ChoiceOption": {"choiceInfo", "defaultIndex"},
    "HostFirewallInfo": {"defaultPolicy", "ruleset"},
    "HostFirewallDefaultPolicy": {"incomingBlocked", "outgoingBlocked"},
    "HostFirewallRuleset": {
        "key",
        "label",
        "required",
        "rule",
        "service",
        "enabled",
        "allowedHosts",
        "userControllable",
        "ipListUserConfigurable",
    },
    "HostFirewallRule": {"port", "endPort", "direction", "portType", "protocol"},
    "HostFirewallRulesetIpList": {"ipAddress", "ipNetwork", "allIp"},
    "HostFirewallRulesetIpNetwork": {"network", "prefixLength"},
    "HostFirewallRulesetRulesetSpec": {"allowedHosts"},
    "UpdateRulesetRequestType": {"id", "spec"},
    "EnableRulesetRequestType": {"id"},
    "DisableRulesetRequestType": {"id"},
    "ChangeLockdownModeRequestType": {"mode"},
    "UpdateLockdownExceptionsRequestType": {"users"},
    "HostSnmpConfigSpec": {"enabled", "port", "readOnlyCommunities", "trapTargets", "option"},
    "HostSnmpDestination": {"hostName", "port", "community"},
    "KeyValue": {"key", "value"},
    "ReconfigureSnmpAgentRequestType": {"spec"},
    "HostDiagnosticPartition": {"storageType", "diagnosticType", "slots", "id"},
    "HostScsiDiskPartition": {"diskName", "partition"},
    "SelectActivePartitionRequestType": {"partition"},
}
_LOCKDOWN_VALUES = ("lockdownDisabled", "lockdownNormal", "lockdownStrict")

#: Fields a file does not have yet. vCenter 8.0 U1 has no ``userControllable``
#: and ``ipListUserConfigurable`` on a rule set; the parser then treats the
#: rule set as changeable and lets vSphere refuse a change it does not allow.
_NOT_IN_FILE: dict[str, dict[str, set[str]]] = {
    "vi-json-8.0u1.yaml": {"HostFirewallRuleset": {"userControllable", "ipListUserConfigurable"}},
}


def test_the_vim_calls_are_pinned() -> None:
    """Shape gate (always runs)."""
    read = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
    assert {
        "vmware.composite.host.advanced_settings.set": (
            read,
            "POST:/OptionManager/{moId}/QueryOptions",
            "POST:/OptionManager/{moId}/UpdateOptions",
        ),
        "vmware.composite.host.firewall.ruleset.set": (
            read,
            "POST:/HostFirewallSystem/{moId}/UpdateRuleset",
            "POST:/HostFirewallSystem/{moId}/EnableRuleset",
            "POST:/HostFirewallSystem/{moId}/DisableRuleset",
        ),
        "vmware.composite.host.lockdown.set": (
            read,
            "POST:/HostAccessManager/{moId}/ChangeLockdownMode",
        ),
        "vmware.composite.host.lockdown.exceptions.set": (
            read,
            "POST:/HostAccessManager/{moId}/QueryLockdownExceptions",
            "POST:/HostAccessManager/{moId}/UpdateLockdownExceptions",
        ),
        "vmware.composite.host.snmp.set": (
            read,
            "POST:/HostSnmpSystem/{moId}/ReconfigureSnmpAgent",
        ),
        "vmware.composite.host.coredump.set": (
            read,
            "POST:/HostDiagnosticSystem/{moId}/QueryAvailablePartition",
            "POST:/HostDiagnosticSystem/{moId}/SelectActivePartition",
        ),
    } == HOST_SECURITY_GOVERNED_SUBOPS


def _all_vim_calls() -> set[str]:
    calls = {OP_QUERY_OPTIONS}
    for sub_ops in HOST_SECURITY_GOVERNED_SUBOPS.values():
        calls |= set(sub_ops)
    return calls


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


def _lines(shelf: str, filename: str) -> list[str]:
    return require_shelf_spec(shelf, filename).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_vim_call_is_served(shelf: str, filename: str) -> None:
    served = _post_paths(_lines(shelf, filename))
    missing = [
        path
        for path in sorted(op.partition(":")[2] for op in _all_vim_calls())
        if path not in served
    ]
    assert not missing, f"{shelf}/{filename} does not serve POST {missing}"


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_property_manager_and_lockdown_value_exists(shelf: str, filename: str) -> None:
    lines = _lines(shelf, filename)
    for mo_type, props in _PROPERTIES.items():
        for prop in props:
            assert f"  /{mo_type}/{{moId}}/{prop}:" in lines, (shelf, filename, mo_type, prop)
    managers = _schema_fields(lines, "HostConfigManager")
    assert managers >= _MANAGERS, sorted(_MANAGERS - managers)
    for value in _LOCKDOWN_VALUES:
        assert {f"        - '{value}'", f"        - {value}"} & set(lines), (shelf, filename, value)


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_body_field_exists(shelf: str, filename: str) -> None:
    lines = _lines(shelf, filename)
    absent = _NOT_IN_FILE.get(filename, {})
    missing = {
        vim_type: sorted(fields - absent.get(vim_type, set()) - _schema_fields(lines, vim_type))
        for vim_type, fields in _FIELDS.items()
    }
    for vim_type, names in absent.items():
        assert not names & _schema_fields(lines, vim_type), (filename, vim_type, "now present")
    missing = {vim_type: names for vim_type, names in missing.items() if names}
    assert not missing, f"{shelf}/{filename} lacks these VIM fields: {missing}"

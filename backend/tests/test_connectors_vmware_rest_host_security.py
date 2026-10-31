# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host advanced settings, firewall, lockdown, SNMP and core dump, against a fake vCenter.

Checks the two reads and the six changes: the request bodies, the read-back,
``unchanged`` (nothing sent), the refusals before a call, a vendor fault, a
change that waits for approval, the approval previews, and that no secret
(SNMP community, a setting that names a password) reaches an answer.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _host_access_writes, _write
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._host_access_writes import (
    host_coredump_set_composite,
    host_lockdown_exceptions_set_composite,
    host_lockdown_set_composite,
    host_snmp_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_writes import (
    host_advanced_settings_set_composite,
    host_firewall_ruleset_set_composite,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_security import (
    advanced_settings_show_impl,
    firewall_show_impl,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from tests._vmware_host_security_fake import (
    HOST,
    OLD_COMMUNITY,
    SECRET_PASSWORD,
    HostSecurityVim,
)
from tests._vmware_teardown_fake import GateRecorder

NEW_COMMUNITY = "new-community-value"
_CHANGES = (
    "vmware.composite.host.advanced_settings.set",
    "vmware.composite.host.firewall.ruleset.set",
    "vmware.composite.host.lockdown.set",
    "vmware.composite.host.lockdown.exceptions.set",
    "vmware.composite.host.snmp.set",
    "vmware.composite.host.coredump.set",
)


def _operator() -> Operator:
    return Operator(
        sub="op-host-security",
        name="Host Security Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004352"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _Target:
    name = "vc-test"
    host = "vc.test.invalid"
    fingerprint: Any = None


class _EsxiTarget(_Target):
    def __init__(self) -> None:
        self.fingerprint = {"product": "esxi", "reachable": True}


def _fault(fault_type: str, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.0.0.0/x")
    response = httpx.Response(
        500, json={"_typeName": fault_type, "faultstring": text}, request=request
    )
    return httpx.HTTPStatusError("500 Server Error", request=request, response=response)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-in for the Vault read of the SNMP community."""
    seen: list[dict[str, Any]] = []

    async def read(operator: Any, target: Any, params: Any, *, prefix: str) -> str:
        seen.append({"prefix": prefix, **params})
        return NEW_COMMUNITY

    monkeypatch.setattr(_host_access_writes, "read_secret_param", read)
    return seen


async def _call(handler: Any, vim: HostSecurityVim, params: dict[str, Any]) -> Any:
    return await handler(operator=_operator(), target=_Target(), params=params, connector=vim)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_advanced_settings_show_marks_changed_values_and_hides_secrets() -> None:
    vim = HostSecurityVim()
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok", out
    rows = {row["key"]: row for row in out["settings"]}
    assert list(rows) == sorted(rows)
    assert rows["Net.BlockGuestBPDU"]["is_default"] is False
    assert rows["Net.BlockGuestBPDU"]["value_type"] == "int"
    assert rows["UserVars.SuppressShellWarning"]["is_default"] is True
    assert rows["UserVars.SuppressShellWarning"]["value_type"] == "long"
    assert rows["Misc.ReadOnlyThing"]["read_only"] is True
    assert rows["UserVars.ExamplePassword"]["value"] == "<hidden>"
    assert SECRET_PASSWORD not in json.dumps(out)
    assert vim.body_of("QueryOptions") == {}


async def test_advanced_settings_show_filters_by_prefix_and_changed_only() -> None:
    vim = HostSecurityVim()
    out = await advanced_settings_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "prefix": "Net.", "changed_only": True},
    )
    assert [row["key"] for row in out["settings"]] == ["Net.BlockGuestBPDU"]
    assert vim.body_of("QueryOptions") == {"name": "Net."}


async def test_advanced_settings_show_unknown_prefix() -> None:
    vim = HostSecurityVim()
    vim.fail["QueryOptions"] = _fault("InvalidName", "no such option")
    out = await advanced_settings_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "prefix": "Nope."},
    )
    assert out["status"] == "no_such_setting"
    assert out["settings"] == []


async def test_advanced_settings_show_without_defaults_still_lists_values() -> None:
    vim = HostSecurityVim()
    vim.definitions = None  # type: ignore[assignment]

    def broken(mo_type: str, prop: str) -> Any:
        raise RuntimeError("defaults unreadable")

    vim._value = broken  # type: ignore[method-assign]
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok"
    assert out["read_notes"] and "defaults" in out["read_notes"][0]
    assert all(row["is_default"] is None for row in out["settings"])


async def test_firewall_show_lists_rulesets_and_filters() -> None:
    vim = HostSecurityVim()
    out = await firewall_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok"
    assert out["default_policy"] == {"incoming_blocked": True, "outgoing_blocked": True}
    ssh = out["rulesets"][0]
    assert ssh["key"] == "sshServer"
    assert ssh["allowed"] == {"all_ip": True, "ip_addresses": [], "networks": []}
    assert ssh["rules"] == [
        {
            "direction": "inbound",
            "protocol": "tcp",
            "port": 22,
            "end_port": None,
            "port_type": "dst",
        }
    ]
    enabled = await firewall_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "enabled_only": True},
    )
    assert [r["key"] for r in enabled["rulesets"]] == ["vpxHeartbeats"]
    missing = await firewall_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "ruleset": "nope"},
    )
    assert missing["status"] == "ruleset_not_found"


async def test_reads_refuse_a_single_esxi_target_and_an_unknown_host() -> None:
    vim = HostSecurityVim()
    out = await firewall_show_impl(vim, _operator(), _EsxiTarget(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "needs_vcenter"
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "nope"})  # type: ignore[arg-type]
    assert out["status"] == "host_not_found"
    assert vim.calls == []


# ---------------------------------------------------------------------------
# host.advanced_settings.set
# ---------------------------------------------------------------------------


async def test_advanced_settings_set_sends_the_typed_value_and_reads_back(
    gate: GateRecorder,
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {
            "host": "esxi-a",
            "settings": [
                {"key": "UserVars.SuppressShellWarning", "value": 1},
                {"key": "Net.BlockGuestBPDU", "value": 1},
            ],
        },
    )
    assert out["status"] == "applied", out
    assert vim.writes() == ["UpdateOptions"]
    assert vim.body_of("UpdateOptions") == {
        "changedValue": [
            {
                "_typeName": "OptionValue",
                "key": "UserVars.SuppressShellWarning",
                "value": {"_typeName": "long", "_value": 1},
            }
        ]
    }
    assert out["before"]["UserVars.SuppressShellWarning"] == 0
    assert out["after"]["UserVars.SuppressShellWarning"] == 1
    assert gate.calls[0]["op_id"] == "POST:/OptionManager/{moId}/UpdateOptions"


async def test_advanced_settings_set_unchanged_sends_nothing(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": "Net.BlockGuestBPDU", "value": 1}]},
    )
    assert out["status"] == "unchanged"
    assert vim.writes() == []
    assert gate.calls == []


@pytest.mark.parametrize(
    ("setting", "status"),
    [
        ({"key": "No.Such.Key", "value": 1}, "unknown_setting"),
        ({"key": "Misc.ReadOnlyThing", "value": "x"}, "read_only_setting"),
        ({"key": "Config.HostAgent.plugins.solo.enableMob", "value": 1}, "invalid_value"),
        ({"key": "UserVars.SuppressShellWarning", "value": 5}, "invalid_value"),
        ({"key": "UserVars.SuppressShellWarning", "value": True}, "invalid_value"),
    ],
)
async def test_advanced_settings_set_checks_before_sending(
    gate: GateRecorder, setting: dict[str, Any], status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite, vim, {"host": "esxi-a", "settings": [setting]}
    )
    assert out["status"] == status, out
    assert out["keys"] == [setting["key"]]
    assert vim.writes() == []


async def test_advanced_settings_set_hides_a_secret_value(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": "UserVars.ExamplePassword", "value": "new-pw"}]},
    )
    assert out["status"] == "applied"
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert SECRET_PASSWORD not in text
    assert "new-pw" not in text


async def test_advanced_settings_set_fault_and_not_verified(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    vim.fail["UpdateOptions"] = _fault("InvalidArgument", "A specified parameter was not correct")
    params = {"host": "esxi-a", "settings": [{"key": "Net.BlockGuestBPDU", "value": 0}]}
    out = await _call(host_advanced_settings_set_composite, vim, params)
    assert out["status"] == "failed"
    assert "InvalidArgument" in out["fault"]
    assert out["issues"][0]["severity"] == "error"
    vim = HostSecurityVim()
    vim.apply = False
    out = await _call(host_advanced_settings_set_composite, vim, params)
    assert out["status"] == "not_verified"


async def test_a_parked_change_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(status="awaiting_approval", op_id="x", result=None, duration_ms=1.0)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked))
    vim = HostSecurityVim()
    out = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "normal"})
    assert out is parked
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# host.firewall.ruleset.set
# ---------------------------------------------------------------------------


async def test_firewall_ruleset_set_limits_addresses_then_turns_on(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_firewall_ruleset_set_composite,
        vim,
        {
            "host": "esxi-a",
            "ruleset": "sshServer",
            "enabled": True,
            "allowed": {"all_ip": False, "networks": ["192.0.2.0/24"], "ip_addresses": []},
        },
    )
    assert out["status"] == "applied", out
    assert vim.writes() == ["UpdateRuleset", "EnableRuleset"]
    assert vim.body_of("UpdateRuleset") == {
        "id": "sshServer",
        "spec": {
            "_typeName": "HostFirewallRulesetRulesetSpec",
            "allowedHosts": {
                "_typeName": "HostFirewallRulesetIpList",
                "allIp": False,
                "ipAddress": [],
                "ipNetwork": [
                    {
                        "_typeName": "HostFirewallRulesetIpNetwork",
                        "network": "192.0.2.0",
                        "prefixLength": 24,
                    }
                ],
            },
        },
    }
    assert out["after"]["enabled"] is True
    assert out["after"]["allowed"]["networks"] == ["192.0.2.0/24"]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"ruleset": "vpxHeartbeats", "enabled": False}, "ruleset_required"),
        ({"ruleset": "nope", "enabled": True}, "ruleset_not_found"),
        ({"ruleset": "sshServer", "allowed": {"all_ip": False}}, "invalid_value"),
        (
            {"ruleset": "sshServer", "allowed": {"all_ip": True, "networks": ["192.0.2.0/24"]}},
            "invalid_value",
        ),
        (
            {"ruleset": "sshServer", "allowed": {"all_ip": False, "networks": ["x/99"]}},
            "invalid_value",
        ),
        ({"ruleset": "sshServer", "enabled": False}, "unchanged"),
    ],
)
async def test_firewall_ruleset_set_refusals(
    gate: GateRecorder, params: dict[str, Any], status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(host_firewall_ruleset_set_composite, vim, {"host": "esxi-a", **params})
    assert out["status"] == status, out
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# Lockdown mode and its exception users
# ---------------------------------------------------------------------------


async def test_lockdown_set_and_unchanged(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "strict"})
    assert out["status"] == "applied"
    assert vim.body_of("ChangeLockdownMode") == {"mode": "lockdownStrict"}
    assert out["before"] == {"mode": "disabled"}
    assert out["after"] == {"mode": "strict"}
    again = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "strict"})
    assert again["status"] == "unchanged"
    assert vim.writes() == ["ChangeLockdownMode"]


async def test_lockdown_exceptions_set(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_lockdown_exceptions_set_composite,
        vim,
        {"host": "esxi-a", "users": ["svc-b", "svc-a"]},
    )
    assert out["status"] == "applied"
    assert vim.body_of("UpdateLockdownExceptions") == {"users": ["svc-a", "svc-b"]}
    assert out["after"] == {"users": ["svc-a", "svc-b"]}
    again = await _call(
        host_lockdown_exceptions_set_composite,
        vim,
        {"host": "esxi-a", "users": ["svc-a", "svc-b"]},
    )
    assert again["status"] == "unchanged"


# ---------------------------------------------------------------------------
# host.snmp.set
# ---------------------------------------------------------------------------


async def test_snmp_set_reads_the_community_from_vault_and_never_shows_it(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_snmp_set_composite,
        vim,
        {
            "host": "esxi-a",
            "enabled": True,
            "community_secret_ref": "snmp/example",
            "trap_targets": [{"host_name": "trap-new.example.invalid"}],
        },
    )
    assert out["status"] == "applied", out
    spec = vim.body_of("ReconfigureSnmpAgent")["spec"]
    assert spec["readOnlyCommunities"] == [NEW_COMMUNITY]
    assert spec["trapTargets"] == [
        {
            "_typeName": "HostSnmpDestination",
            "hostName": "trap-new.example.invalid",
            "port": 162,
            "community": NEW_COMMUNITY,
        }
    ]
    assert spec["option"] == [{"_typeName": "KeyValue", "key": "syscontact", "value": "ops"}]
    assert vault[0]["community_secret_key"] == "community"
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert NEW_COMMUNITY not in text
    assert OLD_COMMUNITY not in text
    assert out["after"]["read_only_community_count"] == 1
    assert out["after"]["trap_targets"] == [{"host_name": "trap-new.example.invalid", "port": 162}]


async def test_snmp_set_keeps_fields_left_out(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(host_snmp_set_composite, vim, {"host": "esxi-a", "enabled": True})
    assert out["status"] == "applied"
    spec = vim.body_of("ReconfigureSnmpAgent")["spec"]
    assert spec["readOnlyCommunities"] == [OLD_COMMUNITY]
    assert spec["trapTargets"][0]["hostName"] == "trap-old.example.invalid"
    assert spec["port"] == 161
    again = await _call(host_snmp_set_composite, vim, {"host": "esxi-a", "enabled": True})
    assert again["status"] == "unchanged"


async def test_snmp_set_needs_a_community_for_traps(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    vim.snmp = {"_typeName": "HostSnmpConfigSpec", "enabled": False, "port": 161}
    out = await _call(
        host_snmp_set_composite,
        vim,
        {
            "host": "esxi-a",
            "enabled": True,
            "trap_targets": [{"host_name": "trap.example.invalid"}],
        },
    )
    assert out["status"] == "community_required"
    assert vim.writes() == []


async def test_snmp_set_hides_the_community_in_a_fault(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.fail["ReconfigureSnmpAgent"] = _fault("InvalidArgument", f"bad community {NEW_COMMUNITY}")
    out = await _call(
        host_snmp_set_composite,
        vim,
        {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"},
    )
    assert out["status"] == "failed"
    assert NEW_COMMUNITY not in json.dumps(out)


# ---------------------------------------------------------------------------
# host.coredump.set
# ---------------------------------------------------------------------------


async def test_coredump_set_picks_a_free_partition(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_coredump_set_composite, vim, {"host": "esxi-a", "disk_name": "disk-b", "partition": 9}
    )
    assert out["status"] == "applied", out
    assert vim.body_of("SelectActivePartition") == {
        "partition": {"_typeName": "HostScsiDiskPartition", "diskName": "disk-b", "partition": 9}
    }
    assert out["after"]["active"]["disk_name"] == "disk-b"


@pytest.mark.parametrize(
    ("disk", "number", "status"),
    [("disk-a", 7, "unchanged"), ("disk-c", 1, "partition_not_available")],
)
async def test_coredump_set_refusals(
    gate: GateRecorder, disk: str, number: int, status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_coredump_set_composite, vim, {"host": "esxi-a", "disk_name": disk, "partition": number}
    )
    assert out["status"] == status
    assert len(out["before"]["available"]) == 2
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# Every change: host refusal, grant set, preview
# ---------------------------------------------------------------------------


async def test_a_change_on_a_single_esxi_target_needs_vcenter(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await host_lockdown_set_composite(
        operator=_operator(), target=_EsxiTarget(), params={"mode": "normal"}, connector=vim
    )
    assert out["status"] == "needs_vcenter"
    assert vim.calls == []


def test_every_change_lists_its_vim_calls() -> None:
    for op_id in _CHANGES:
        calls = _GOVERNED_SUBOP_MANIFEST[op_id]
        assert calls[0] == "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
        assert len(calls) >= 2


def _preview_ctx(vim: HostSecurityVim, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=vim,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


@pytest.mark.parametrize(
    ("op_id", "params", "current"),
    [
        ("vmware.composite.host.lockdown.set", {"mode": "normal"}, {"mode": "disabled"}),
        ("vmware.composite.host.lockdown.exceptions.set", {"users": []}, {"users": ["svc-old"]}),
        (
            "vmware.composite.host.advanced_settings.set",
            {"settings": [{"key": "UserVars.ExamplePassword", "value": "x"}]},
            {"UserVars.ExamplePassword": "<hidden>"},
        ),
    ],
)
async def test_previews_show_the_current_setting(
    op_id: str, params: dict[str, Any], current: Any
) -> None:
    vim = HostSecurityVim()
    preview = await _PREVIEW_BUILDERS[op_id](_preview_ctx(vim, {"host": "esxi-a", **params}))
    assert preview is not None
    assert preview["host"] == HOST
    assert preview["current"] == current
    assert vim.writes() == []


async def test_the_snmp_preview_shows_no_community() -> None:
    vim = HostSecurityVim()
    params = {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"}
    preview = await _PREVIEW_BUILDERS["vmware.composite.host.snmp.set"](_preview_ctx(vim, params))
    assert preview is not None
    assert preview["requested"]["community_secret_ref"] == "snmp/example"
    assert preview["current"]["read_only_community_count"] == 1
    assert OLD_COMMUNITY not in json.dumps(preview)

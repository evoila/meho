# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the operations that build a domain controller.

They cover the new operations of three SSH connectors:

* windns: ``zone.get`` / ``zone.set`` / ``forwarder.list`` / ``forwarder.set``;
* winsrv: ``time.get`` / ``time.set``;
* msad: ``group.create``, ``ou.grant-create-child`` / ``ou.revoke-create-child``,
  ``domain.settings``, ``domain.password-policy.set`` and
  ``domain.machine-account-quota.set``.

No Windows server is needed. ``_run_command`` is replaced by a fake that
returns fixed JSON. The tests decode the ``-EncodedCommand`` payload back to
the PowerShell text and check the script each handler builds, how operator
input is quoted, and which input is refused before anything runs.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.msad import MsadConnector
from meho_backplane.connectors.msad.ops_domain_config import (
    msad_domain_machine_account_quota_set,
    msad_domain_password_policy_set,
    msad_domain_settings,
)
from meho_backplane.connectors.msad.ops_groups import msad_group_create
from meho_backplane.connectors.msad.ops_ou_access import (
    msad_ou_grant_create_child,
    msad_ou_revoke_create_child,
)
from meho_backplane.connectors.windows_dns import WindowsDnsConnector
from meho_backplane.connectors.windows_dns.ops_forwarder import (
    windows_dns_forwarder_list,
    windows_dns_forwarder_set,
)
from meho_backplane.connectors.windows_dns.ops_zone_settings import (
    windows_dns_zone_get,
    windows_dns_zone_set,
)
from meho_backplane.connectors.winsrv import WinsrvConnector
from meho_backplane.connectors.winsrv.ops_time import winsrv_time_get, winsrv_time_set
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="dc-test",
    host="dc.test.invalid",
    port=22,
    secret_ref="meho/testing/dc/dc-test",
)


def _run(payload: dict[str, Any]) -> AsyncMock:
    proc = MagicMock()
    proc.stdout = json.dumps(payload)
    proc.stderr = ""
    proc.exit_status = 0
    return AsyncMock(return_value=proc)


def _script(run_mock: AsyncMock) -> str:
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")


async def _call(connector: Any, handler: Any, params: dict[str, Any], payload: dict[str, Any]):
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(connector, _TARGET, params)
    return result, _script(run_mock)


# ---------------------------------------------------------------------------
# windns: zone settings
# ---------------------------------------------------------------------------

_ZONE = {
    "zone": "example.test",
    "zone_type": "Primary",
    "dynamic_update": "Secure",
    "is_ds_integrated": True,
    "replication_scope": "Domain",
    "soa_minimum_ttl_seconds": 300,
}


async def test_zone_get_reads_zone_and_soa() -> None:
    result, script = await _call(
        WindowsDnsConnector(), windows_dns_zone_get, {"zone": "example.test"}, _ZONE
    )
    assert result == _ZONE
    assert "$ErrorActionPreference = 'Stop';" in script
    assert "Get-DnsServerZone -Name 'example.test'" in script
    assert "-RRType Soa -Name '@'" in script
    assert "$d.MinimumTimeToLive.TotalSeconds" in script


async def test_zone_set_dynamic_update_only() -> None:
    result, script = await _call(
        WindowsDnsConnector(),
        windows_dns_zone_set,
        {"zone": "example.test", "dynamic_update": "Secure"},
        _ZONE,
    )
    assert "Set-DnsServerPrimaryZone -Name 'example.test' -DynamicUpdate 'Secure';" in script
    assert "Set-DnsServerResourceRecord" not in script
    assert result["op_class"] == "write"
    assert result["dynamic_update"] == "Secure"


async def test_zone_set_soa_minimum_ttl_clones_the_record() -> None:
    _, script = await _call(
        WindowsDnsConnector(),
        windows_dns_zone_set,
        {"zone": "example.test", "soa_minimum_ttl_seconds": 300},
        _ZONE,
    )
    assert "Set-DnsServerPrimaryZone" not in script
    assert "$new = $old.Clone();" in script
    assert "MinimumTimeToLive = [System.TimeSpan]::FromSeconds(300);" in script
    assert "-OldInputObject $old -NewInputObject $new" in script


async def test_zone_set_quotes_the_zone_name() -> None:
    _, script = await _call(
        WindowsDnsConnector(),
        windows_dns_zone_set,
        {"zone": "ex'ample.test", "dynamic_update": "None"},
        _ZONE,
    )
    assert "-Name 'ex''ample.test'" in script


@pytest.mark.parametrize(
    "params",
    [
        {"zone": "example.test"},
        {"zone": "example.test", "dynamic_update": "Anyone"},
        {"zone": "example.test", "soa_minimum_ttl_seconds": -1},
        {"zone": "example.test", "soa_minimum_ttl_seconds": 86401},
        {"zone": "example.test", "soa_minimum_ttl_seconds": "300"},
        {"zone": "example.test", "soa_minimum_ttl_seconds": True},
    ],
)
async def test_zone_set_refuses_bad_input_before_running(params: dict[str, Any]) -> None:
    connector = WindowsDnsConnector()
    run_mock = _run(_ZONE)
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await windows_dns_zone_set(connector, _TARGET, params)
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# windns: forwarders
# ---------------------------------------------------------------------------


async def test_forwarder_list_turns_one_address_into_a_list() -> None:
    result, script = await _call(
        WindowsDnsConnector(),
        windows_dns_forwarder_list,
        {},
        {"ip_addresses": "192.0.2.53", "use_root_hint": True},
    )
    assert result["ip_addresses"] == ["192.0.2.53"]
    assert "Get-DnsServerForwarder" in script


async def test_forwarder_set_replaces_the_list() -> None:
    result, script = await _call(
        WindowsDnsConnector(),
        windows_dns_forwarder_set,
        {"ip_addresses": ["192.0.2.53", "2001:db8::53"], "use_root_hint": False},
        {"ip_addresses": ["192.0.2.53", "2001:db8::53"], "use_root_hint": False},
    )
    assert (
        "Set-DnsServerForwarder -IPAddress @('192.0.2.53', '2001:db8::53') -UseRootHint $false;"
        in script
    )
    assert result["op_class"] == "write"


async def test_forwarder_set_leaves_root_hint_alone_when_not_given() -> None:
    _, script = await _call(
        WindowsDnsConnector(),
        windows_dns_forwarder_set,
        {"ip_addresses": ["192.0.2.53"]},
        {"ip_addresses": ["192.0.2.53"]},
    )
    assert "-UseRootHint" not in script


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["dns.example.test"],
        ["192.0.2.53'; Remove-Item C:\\x; '"],
        ["192.0.2.53", "192.0.2.53"],
        [f"192.0.2.{n}" for n in range(1, 10)],
    ],
)
async def test_forwarder_set_refuses_anything_but_ip_addresses(addresses: list[str]) -> None:
    connector = WindowsDnsConnector()
    run_mock = _run({})
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await windows_dns_forwarder_set(connector, _TARGET, {"ip_addresses": addresses})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# winsrv: time source
# ---------------------------------------------------------------------------


async def test_time_get_splits_peers_and_reads_the_reliable_flag() -> None:
    result, script = await _call(
        WinsrvConnector(),
        winsrv_time_get,
        {},
        {
            "ntp_server": "ntp1.example.test,0x8 192.0.2.123,0x8",
            "type": "NTP",
            "announce_flags": 5,
            "source": "ntp1.example.test,0x8",
        },
    )
    assert result["peers"] == ["ntp1.example.test,0x8", "192.0.2.123,0x8"]
    assert result["reliable"] is True
    assert "W32Time\\Parameters" in script
    assert "w32tm /query /source" in script


async def test_time_get_not_reliable_with_default_flags() -> None:
    result, _ = await _call(
        WinsrvConnector(),
        winsrv_time_get,
        {},
        {"ntp_server": "", "type": "NT5DS", "announce_flags": 10, "source": None},
    )
    assert result["reliable"] is False
    assert result["peers"] == []


async def test_time_set_runs_w32tm_config_and_checks_the_exit_code() -> None:
    result, script = await _call(
        WinsrvConnector(),
        winsrv_time_set,
        {"peers": ["ntp1.example.test,0x8", "192.0.2.123"], "reliable": True},
        {"ntp_server": "ntp1.example.test,0x8 192.0.2.123", "type": "NTP", "announce_flags": 5},
    )
    assert "$peers = 'ntp1.example.test,0x8 192.0.2.123';" in script
    assert (
        '& w32tm /config "/manualpeerlist:$peers" /syncfromflags:manual /reliable:yes /update'
        in script
    )
    assert "if ($LASTEXITCODE -ne 0) { throw" in script
    assert result["op_class"] == "write"
    assert result["peers"] == ["ntp1.example.test,0x8", "192.0.2.123"]


async def test_time_set_without_reliable_keeps_the_flag() -> None:
    _, script = await _call(
        WinsrvConnector(),
        winsrv_time_set,
        {"peers": ["ntp1.example.test"]},
        {"ntp_server": "ntp1.example.test", "type": "NTP", "announce_flags": 10},
    )
    assert "/reliable:" not in script


@pytest.mark.parametrize(
    "peers",
    [
        [],
        ["ntp1.example.test ntp2.example.test"],
        ["ntp'1.example.test"],
        ["$(Get-Process)"],
        ["ntp1.example.test,0x8,0x2"],
        ["a.example.test", "a.example.test"],
        [f"ntp{n}.example.test" for n in range(6)],
    ],
)
async def test_time_set_refuses_bad_peers(peers: list[str]) -> None:
    connector = WinsrvConnector()
    run_mock = _run({})
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await winsrv_time_set(connector, _TARGET, {"peers": peers})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# msad: group create
# ---------------------------------------------------------------------------

_GROUP = {
    "created": True,
    "sam_account_name": "Server-Joiners",
    "name": "Server-Joiners",
    "distinguished_name": "CN=Server-Joiners,OU=Groups,DC=example,DC=test",
    "scope": "Global",
    "category": "Security",
}


async def test_group_create_defaults_and_existence_check() -> None:
    result, script = await _call(
        MsadConnector(), msad_group_create, {"name": "Server-Joiners"}, _GROUP
    )
    assert "$n = 'Server-Joiners';" in script
    assert "Get-ADGroup -Filter { SamAccountName -eq $n }" in script
    assert "if (-not $g) { New-ADGroup -Name 'Server-Joiners' -SamAccountName $n" in script
    assert "-GroupScope Global -GroupCategory Security;" in script
    assert "-Path" not in script
    assert result["action"] == "create"
    assert result["op_class"] == "write"


async def test_group_create_quotes_path_and_description() -> None:
    _, script = await _call(
        MsadConnector(),
        msad_group_create,
        {
            "name": "SQL Admins",
            "sam_account_name": "sql-admins",
            "scope": "DomainLocal",
            "category": "Distribution",
            "path": "OU=Gr'oups,DC=example,DC=test",
            "description": "Admins' group",
        },
        _GROUP,
    )
    assert "$n = 'sql-admins';" in script
    assert "-Name 'SQL Admins'" in script
    assert "-GroupScope DomainLocal -GroupCategory Distribution" in script
    assert "-Path 'OU=Gr''oups,DC=example,DC=test'" in script
    assert "-Description 'Admins'' group'" in script


@pytest.mark.parametrize(
    "params",
    [
        {"name": "G", "scope": "Global; Remove-ADGroup x"},
        {"name": "G", "category": "Other"},
    ],
)
async def test_group_create_refuses_unknown_scope_or_category(params: dict[str, Any]) -> None:
    connector = MsadConnector()
    run_mock = _run(_GROUP)
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_group_create(connector, _TARGET, params)
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# msad: access rule on an OU
# ---------------------------------------------------------------------------

_ACL = {
    "path": "OU=Servers,DC=example,DC=test",
    "group_sid": "S-1-5-21-1-2-3-1104",
    "object_class": "computer",
    "changed": True,
    "rule_present": True,
}


async def test_grant_create_child_adds_one_rule_only_when_missing() -> None:
    result, script = await _call(
        MsadConnector(),
        msad_ou_grant_create_child,
        {"path": "OU=Servers,DC=example,DC=test", "group": "Server-Joiners"},
        _ACL,
    )
    assert "Get-ADObject -Identity 'OU=Servers,DC=example,DC=test'" in script
    assert "(Get-ADGroup -Identity 'Server-Joiners').SID" in script
    assert "$cls = 'computer';" in script
    assert "schemaNamingContext" in script
    assert "ActiveDirectorySecurityInheritance]::All" in script
    assert "if ($have.Count -eq 0) {" in script
    assert "$sid, $rights, 'Allow', $guid, $inh, $anyChild);" in script
    assert "$acl.AddAccessRule($rule); Set-Acl -LiteralPath $adPath -AclObject $acl;" in script
    assert result["action"] == "grant-create-child"
    assert result["group"] == "Server-Joiners"
    assert result["rule_present"] is True
    assert result["op_class"] == "write"


async def test_access_rule_match_covers_the_whole_inheritance_scope() -> None:
    """A rule that reaches only one child type is narrower and must not match."""
    for handler in (msad_ou_grant_create_child, msad_ou_revoke_create_child):
        _, script = await _call(
            MsadConnector(),
            handler,
            {"path": "OU=Servers,DC=example,DC=test", "group": "Server-Joiners"},
            _ACL,
        )
        assert "$anyChild = [Guid]::Empty;" in script
        assert (
            "$_.IdentityReference -eq $sid -and $_.ObjectType -eq $guid -and "
            "$_.InheritedObjectType -eq $anyChild -and "
            "$_.AccessControlType -eq 'Allow' -and $_.InheritanceType -eq $inh"
        ) in script
        # The rights are not part of the match: a rule may hold more than CreateChild.
        assert "$_.ActiveDirectoryRights -eq $rights" not in script
        assert "([int]$r.ActiveDirectoryRights -band [int]$rights) -eq [int]$rights" in script


async def test_grant_create_child_treats_a_wider_rule_as_present() -> None:
    _, script = await _call(
        MsadConnector(),
        msad_ou_grant_create_child,
        {"path": "OU=Servers,DC=example,DC=test", "group": "Server-Joiners"},
        _ACL,
    )
    assert "$have = @($rules | Where-Object { & $hasRight $_ });" in script


async def test_revoke_create_child_keeps_the_other_rights_of_a_matched_rule() -> None:
    result, script = await _call(
        MsadConnector(),
        msad_ou_revoke_create_child,
        {
            "path": "OU=Servers,DC=example,DC=test",
            "group": "Server-Joiners",
            "object_class": "user",
        },
        {**_ACL, "object_class": "user", "changed": False, "rule_present": False},
    )
    assert "$cls = 'user';" in script
    assert "foreach ($r in @($rules | Where-Object { & $hasRight $_ })) {" in script
    assert "[void]$acl.RemoveAccessRuleSpecific($r);" in script
    assert "$acl.RemoveAccessRule(" not in script
    assert "$rest = [int]$r.ActiveDirectoryRights -band (-bnot [int]$rights);" in script
    assert (
        "if ($rest -ne 0) { "
        "$keep = New-Object System.DirectoryServices.ActiveDirectoryAccessRule("
        "$sid, [System.DirectoryServices.ActiveDirectoryRights]$rest, 'Allow', "
        "$r.ObjectType, $r.InheritanceType, $r.InheritedObjectType); "
        "$acl.AddAccessRule($keep) };"
    ) in script
    assert "if ($changed) { Set-Acl -LiteralPath $adPath -AclObject $acl };" in script
    assert result["action"] == "revoke-create-child"
    assert result["changed"] is False
    assert result["rule_present"] is False


async def test_access_rule_ops_read_the_saved_permissions_back() -> None:
    read_back = (
        "$after = & $find (Get-Acl -LiteralPath $adPath); "
        "$present = @($after | Where-Object { & $hasRight $_ }).Count -gt 0;"
    )
    params = {"path": "OU=Servers,DC=example,DC=test", "group": "Server-Joiners"}
    _, grant = await _call(MsadConnector(), msad_ou_grant_create_child, params, _ACL)
    _, revoke = await _call(MsadConnector(), msad_ou_revoke_create_child, params, _ACL)
    for script in (grant, revoke):
        assert read_back in script
        save = "Set-Acl -LiteralPath $adPath -AclObject $acl"
        assert script.index(read_back) > script.index(save)
        assert "rule_present = $present }" in script
    assert "if (-not $present) { throw 'the access rule is not in the saved permissions' }" in grant
    assert "if ($present) { throw 'the access rule is still in the saved permissions' }" in revoke


async def test_access_rule_quotes_operator_input() -> None:
    _, script = await _call(
        MsadConnector(),
        msad_ou_grant_create_child,
        {"path": "OU=O'Brien,DC=example,DC=test", "group": "g'1"},
        _ACL,
    )
    assert "-Identity 'OU=O''Brien,DC=example,DC=test'" in script
    assert "-Identity 'g''1'" in script


async def test_access_rule_refuses_other_object_classes() -> None:
    connector = MsadConnector()
    run_mock = _run(_ACL)
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_ou_grant_create_child(
            connector,
            _TARGET,
            {"path": "OU=Servers,DC=example,DC=test", "group": "g", "object_class": "*"},
        )
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# msad: domain settings
# ---------------------------------------------------------------------------

_SETTINGS = {
    "domain": "example.test",
    "machine_account_quota": 0,
    "password_policy": {"min_length": 14, "max_age_days": 0, "complexity_enabled": True},
}


async def test_domain_settings_reads_policy_and_join_limit() -> None:
    result, script = await _call(MsadConnector(), msad_domain_settings, {}, _SETTINGS)
    assert result == _SETTINGS
    assert "Get-ADDefaultDomainPasswordPolicy -Identity $d.DistinguishedName" in script
    assert "'ms-DS-MachineAccountQuota'" in script


async def test_password_policy_set_builds_typed_arguments() -> None:
    result, script = await _call(
        MsadConnector(),
        msad_domain_password_policy_set,
        {
            "min_length": 14,
            "max_age_days": 0,
            "lockout_duration_minutes": 15,
            "complexity_enabled": True,
        },
        _SETTINGS,
    )
    assert (
        "Set-ADDefaultDomainPasswordPolicy -Identity $dn -MinPasswordLength 14 "
        "-MaxPasswordAge ([System.TimeSpan]::FromDays(0)) "
        "-LockoutDuration ([System.TimeSpan]::FromMinutes(15)) -ComplexityEnabled $true;"
    ) in script
    assert "-ReversibleEncryptionEnabled" not in script
    assert result["op_class"] == "write"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"min_length": 200},
        {"history_count": -1},
        {"max_age_days": "42"},
        {"lockout_threshold": True},
        {"complexity_enabled": "yes"},
    ],
)
async def test_password_policy_set_refuses_bad_input(params: dict[str, Any]) -> None:
    connector = MsadConnector()
    run_mock = _run(_SETTINGS)
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_domain_password_policy_set(connector, _TARGET, params)
    run_mock.assert_not_awaited()


async def test_machine_account_quota_set_replaces_the_attribute() -> None:
    result, script = await _call(
        MsadConnector(), msad_domain_machine_account_quota_set, {"quota": 0}, _SETTINGS
    )
    assert "Set-ADDomain -Identity $dn -Replace @{'ms-DS-MachineAccountQuota' = 0};" in script
    assert result["machine_account_quota"] == 0
    assert result["op_class"] == "write"


@pytest.mark.parametrize("quota", [-1, 1001, "0", None, False])
async def test_machine_account_quota_set_refuses_bad_values(quota: Any) -> None:
    connector = MsadConnector()
    run_mock = _run(_SETTINGS)
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_domain_machine_account_quota_set(connector, _TARGET, {"quota": quota})
    run_mock.assert_not_awaited()

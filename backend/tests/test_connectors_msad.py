# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the msad connector (SSH → PowerShell / Active Directory).

Mirrors the winsrv harness (``test_connectors_winsrv.py``): a ``_StubTarget``
dataclass, a ``_completed_process`` SSHCompletedProcess stub, and
``patch.object(connector, "_run_command", AsyncMock(...))`` so the AD-cmdlet
handlers can be exercised without a real domain controller. Because the
transport is ``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell
5.1), the assertions decode the base64 payload back to the script and assert on
the cmdlet + quoted arguments the handler built.

There is no spec-reconcile lane — the cmdlet surface ships no OpenAPI (the
confirmed convention for SSH-typed connectors). Drift protection is this
ordinary unit suite. Coverage spans every op group, the injection-safety escape
on every parameterized script, the search script-block filter form, and the
secret-hygiene invariant that no op exposes a secret-value parameter (password
provisioning is deferred — see the connector doc).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.msad  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.msad import MSAD_OPS, MsadConnector
from meho_backplane.connectors.msad.ops import MSAD_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.msad.ops_computers import (
    msad_computer_delete,
    msad_computer_get,
    msad_computer_join_prestage,
    msad_computer_list,
    msad_computer_unjoin,
)
from meho_backplane.connectors.msad.ops_domain import (
    msad_domain_controllers,
    msad_domain_forest,
    msad_domain_info,
    msad_domain_replication,
)
from meho_backplane.connectors.msad.ops_groups import (
    msad_group_add_member,
    msad_group_delete,
    msad_group_get,
    msad_group_list,
    msad_group_members,
    msad_group_remove_member,
)
from meho_backplane.connectors.msad.ops_ou import (
    msad_ou_create,
    msad_ou_list,
    msad_ou_move,
)
from meho_backplane.connectors.msad.ops_users import (
    msad_user_create,
    msad_user_delete,
    msad_user_disable,
    msad_user_enable,
    msad_user_get,
    msad_user_list,
    msad_user_search,
    msad_user_set,
)
from meho_backplane.connectors.registry import product_impl_id_round_trips
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires (mirrors the winsrv suite)."""
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
    name="msad-test",
    host="dc.test.invalid",
    port=22,
    secret_ref="meho/testing/msad/msad-test",
)


def _completed_process(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's :class:`SSHCompletedProcess`."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _script_from_call(run_mock: AsyncMock) -> str:
    """Recover the PowerShell script from the mocked ``_run_command`` argv."""
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    encoded = cmd.rsplit(" ", 1)[1]
    return base64.b64decode(encoded).decode("utf-16-le")


def _run(stdout: str) -> AsyncMock:
    return AsyncMock(return_value=_completed_process(stdout=stdout))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


async def test_transport_uses_windows_powershell_and_suppresses_progress() -> None:
    connector = MsadConnector()
    assert connector.POWERSHELL_EXECUTABLE == "powershell"
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_list(connector, _TARGET, {})
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert not cmd.startswith("pwsh ")
    assert _script_from_call(run_mock).startswith("$ProgressPreference = 'SilentlyContinue';")


# ---------------------------------------------------------------------------
# Registration / metadata invariants
# ---------------------------------------------------------------------------


def test_connector_id_triple_round_trips() -> None:
    assert MsadConnector.product == "msad"
    assert MsadConnector.version == "2022.x"
    assert MsadConnector.impl_id == "msad-ssh"
    assert product_impl_id_round_trips(product="msad", version="2022.x", impl_id="msad-ssh")


def test_op_surface_ids_and_safety_tiers() -> None:
    by_id = {op.op_id: op for op in MSAD_OPS}
    assert len(by_id) == 27
    dangerous = {k for k, v in by_id.items() if v.safety_level == "dangerous"}
    assert dangerous == {
        "msad.user.delete",
        "msad.group.delete",
        "msad.computer.delete",
    }
    # Every dangerous op requires approval; nothing else does.
    assert {k for k, v in by_id.items() if v.requires_approval} == dangerous
    caution = {k for k, v in by_id.items() if v.safety_level == "caution"}
    assert caution == {
        "msad.user.create",
        "msad.user.set",
        "msad.user.enable",
        "msad.user.disable",
        "msad.group.add-member",
        "msad.group.remove-member",
        "msad.computer.join-prestage",
        "msad.computer.unjoin",
        "msad.ou.create",
        "msad.ou.move",
    }
    safe = {k for k, v in by_id.items() if v.safety_level == "safe"}
    assert len(safe) == 14
    assert {"msad.about", "msad.domain.info", "msad.user.list"} <= safe


def test_every_group_has_a_curated_when_to_use() -> None:
    groups = {op.group_key for op in MSAD_OPS}
    assert groups == {"domain", "users", "groups", "computers", "ou"}
    assert groups <= set(MSAD_WHEN_TO_USE_BY_GROUP)


def test_every_handler_attr_resolves_on_the_class() -> None:
    for op in MSAD_OPS:
        assert getattr(MsadConnector, op.handler_attr, None) is not None, op.op_id


def test_no_op_exposes_a_secret_value_field() -> None:
    """The secret-leak guard at the schema level: no op accepts a plaintext
    secret-value parameter, so a secret can never reach the ``-EncodedCommand``
    argv. Password-reset / password-set is deliberately not shipped — a
    plaintext AD password cannot ride the pwsh transport (see the connector
    doc); it is a Vault-brokered follow-up."""
    secret_fields = {
        "password",
        "account_password",
        "new_password",
        "secret",
        "credential",
        "chap_secret",
    }
    for op in MSAD_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


def test_no_op_id_is_password_reset() -> None:
    """Password-reset is deferred by design (secret-hygiene contract)."""
    ids = {op.op_id for op in MSAD_OPS}
    assert "msad.user.password-reset" not in ids
    assert not any("password" in op_id for op_id in ids)


# ---------------------------------------------------------------------------
# domain group (constant scripts — no injection surface)
# ---------------------------------------------------------------------------


async def test_domain_info_projects_fsmo_roles() -> None:
    connector = MsadConnector()
    run_mock = _run('{"DNSRoot":"c1sql.lab","PDCEmulator":"dc1.c1sql.lab"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_domain_info(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ADDomain" in script
    assert "PDCEmulator = $d.PDCEmulator" in script
    assert "InfrastructureMaster = $d.InfrastructureMaster" in script
    assert result["DNSRoot"] == "c1sql.lab"


async def test_domain_forest_projects_forest_fsmo() -> None:
    connector = MsadConnector()
    run_mock = _run('{"Name":"c1sql.lab","SchemaMaster":"dc1.c1sql.lab"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_domain_forest(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ADForest" in script
    assert "SchemaMaster = $f.SchemaMaster" in script
    assert "DomainNamingMaster = $f.DomainNamingMaster" in script
    assert result["Name"] == "c1sql.lab"


async def test_domain_controllers_builds_list_envelope() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[{"Name":"DC1"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_domain_controllers(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ADDomainController -Filter *" in script
    assert "rows = $x; total = $x.Count" in script
    assert result["total"] == 1


@pytest.mark.parametrize("rows_json", ["[]", "null"])
async def test_domain_replication_empty_single_dc(rows_json: str) -> None:
    connector = MsadConnector()
    run_mock = _run(f'{{"rows":{rows_json},"total":0}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_domain_replication(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ADReplicationPartnerMetadata -Target $dom -Scope Domain" in script
    assert result == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# users group
# ---------------------------------------------------------------------------


async def test_user_list_caps_result_set() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_list(connector, _TARGET, {"limit": 25})
    script = _script_from_call(run_mock)
    assert "Get-ADUser -Filter * -ResultSetSize 25" in script


async def test_user_list_default_limit_is_500() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_list(connector, _TARGET, {})
    assert "-ResultSetSize 500" in _script_from_call(run_mock)


@pytest.mark.parametrize("bad", [0, -1, "10", 3.5, True])
async def test_user_list_rejects_bad_limit(bad: Any) -> None:
    connector = MsadConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_user_list(connector, _TARGET, {"limit": bad})
    run_mock.assert_not_awaited()


async def test_user_get_escapes_identity() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[{"SamAccountName":"o\'db"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_user_get(connector, _TARGET, {"identity": "o'db"})
    script = _script_from_call(run_mock)
    assert "Get-ADUser -Identity 'o''db'" in script  # single quote doubled
    assert result["identity"] == "o'db"


async def test_user_search_uses_scriptblock_filter_and_escapes() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_search(connector, _TARGET, {"query": "sq'l"})
    script = _script_from_call(run_mock)
    # The query is a single-quoted literal (embedded quote doubled), wrapped in
    # wildcards, and bound via a script-block filter (injection-safe form).
    assert "$q = '*sq''l*';" in script
    assert "Get-ADUser -Filter {(Name -like $q) -or (SamAccountName -like $q)}" in script


async def test_user_create_is_passwordless_and_disabled() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true,"user":{"SamAccountName":"svc-sql","Enabled":false}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_user_create(
            connector,
            _TARGET,
            {"name": "svc sql", "sam_account_name": "svc-sql", "description": "SQL svc"},
        )
    script = _script_from_call(run_mock)
    assert "New-ADUser -Name 'svc sql' -SamAccountName 'svc-sql'" in script
    assert "-Description 'SQL svc'" in script
    # Secret-hygiene: no password material ever enters the script.
    assert "-AccountPassword" not in script
    assert "ConvertTo-SecureString" not in script
    assert result["identity"] == "svc-sql"
    assert result["action"] == "create"


async def test_user_create_escapes_optional_fields() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true,"user":{}}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_create(
            connector,
            _TARGET,
            {
                "name": "n",
                "sam_account_name": "s",
                "path": "OU=x,DC=c1sql,DC=lab",
                "user_principal_name": "u'p@c1sql.lab",
            },
        )
    script = _script_from_call(run_mock)
    assert "-Path 'OU=x,DC=c1sql,DC=lab'" in script
    assert "-UserPrincipalName 'u''p@c1sql.lab'" in script


async def test_user_set_requires_an_attribute() -> None:
    connector = MsadConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_user_set(connector, _TARGET, {"identity": "svc-sql"})
    run_mock.assert_not_awaited()


async def test_user_set_builds_setaduser_never_password() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true,"user":{}}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_user_set(
            connector, _TARGET, {"identity": "svc-sql", "description": "x", "email": "a@b"}
        )
    script = _script_from_call(run_mock)
    assert "Set-ADUser -Identity 'svc-sql' -Description 'x' -EmailAddress 'a@b'" in script
    assert "-AccountPassword" not in script


@pytest.mark.parametrize(
    ("handler", "cmdlet", "action", "confirm"),
    [
        (msad_user_enable, "Enable-ADAccount", "enable", False),
        (msad_user_disable, "Disable-ADAccount", "disable", False),
        (msad_user_delete, "Remove-ADUser", "delete", True),
    ],
)
async def test_user_account_actions(handler: Any, cmdlet: str, action: str, confirm: bool) -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(connector, _TARGET, {"identity": "svc-sql"})
    script = _script_from_call(run_mock)
    assert f"{cmdlet} -Identity 'svc-sql'" in script
    assert ("-Confirm:$false" in script) is confirm
    assert result == {"identity": "svc-sql", "action": action, "op_class": "write"}


# ---------------------------------------------------------------------------
# groups group
# ---------------------------------------------------------------------------


async def test_group_list_and_get_and_members() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[{"Name":"SQL-Admins"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_group_list(connector, _TARGET, {})
        assert "Get-ADGroup -Filter *" in _script_from_call(run_mock)
        result = await msad_group_get(connector, _TARGET, {"identity": "SQL-Admins"})
        assert "Get-ADGroup -Identity 'SQL-Admins'" in _script_from_call(run_mock)
        assert result["identity"] == "SQL-Admins"
        members = await msad_group_members(connector, _TARGET, {"identity": "SQL-Admins"})
        assert "Get-ADGroupMember -Identity 'SQL-Admins'" in _script_from_call(run_mock)
        assert members["identity"] == "SQL-Admins"


@pytest.mark.parametrize(
    ("handler", "cmdlet", "action"),
    [
        (msad_group_add_member, "Add-ADGroupMember", "add-member"),
        (msad_group_remove_member, "Remove-ADGroupMember", "remove-member"),
    ],
)
async def test_group_member_writes_build_array_and_escape(
    handler: Any, cmdlet: str, action: str
) -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(
            connector, _TARGET, {"identity": "SQL-Admins", "members": ["svc-sql", "o'db"]}
        )
    script = _script_from_call(run_mock)
    assert (
        f"{cmdlet} -Identity 'SQL-Admins' -Members @('svc-sql', 'o''db') -Confirm:$false" in script
    )
    assert result == {
        "identity": "SQL-Admins",
        "action": action,
        "members": ["svc-sql", "o'db"],
        "op_class": "write",
    }


@pytest.mark.parametrize("members", [[], ["ok", ""], ["ok", 5]])
async def test_group_add_member_rejects_bad_members(members: Any) -> None:
    connector = MsadConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await msad_group_add_member(connector, _TARGET, {"identity": "g", "members": members})
    run_mock.assert_not_awaited()


async def test_group_delete_builds_remove_with_confirm() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_group_delete(connector, _TARGET, {"identity": "SQL-Admins"})
    script = _script_from_call(run_mock)
    assert "Remove-ADGroup -Identity 'SQL-Admins' -Confirm:$false" in script
    assert result == {"identity": "SQL-Admins", "action": "delete", "op_class": "write"}


# ---------------------------------------------------------------------------
# computers group
# ---------------------------------------------------------------------------


async def test_computer_list_and_get() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[{"Name":"SQLNODE1"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        await msad_computer_list(connector, _TARGET, {})
        assert "Get-ADComputer -Filter *" in _script_from_call(run_mock)
        result = await msad_computer_get(connector, _TARGET, {"identity": "SQLNODE1"})
        assert "Get-ADComputer -Identity 'SQLNODE1'" in _script_from_call(run_mock)
        assert result["identity"] == "SQLNODE1"


async def test_computer_join_prestage_builds_new_adcomputer() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true,"computer":{"Name":"SQLNODE1"}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_computer_join_prestage(
            connector, _TARGET, {"name": "SQLNODE1", "path": "OU=Servers,DC=c1sql,DC=lab"}
        )
    script = _script_from_call(run_mock)
    assert "New-ADComputer -Name 'SQLNODE1'" in script
    assert "-Path 'OU=Servers,DC=c1sql,DC=lab'" in script
    # No explicit SAM → read the account back by the CN.
    assert "Get-ADComputer -Identity 'SQLNODE1'" in script
    assert result["action"] == "join-prestage"
    assert result["identity"] == "SQLNODE1"


async def test_computer_join_prestage_reads_back_by_explicit_sam() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true,"computer":{"Name":"SQLNODE1"}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_computer_join_prestage(
            connector, _TARGET, {"name": "SQLNODE1", "sam_account_name": "SQLALT$"}
        )
    script = _script_from_call(run_mock)
    assert "New-ADComputer -Name 'SQLNODE1' -SamAccountName 'SQLALT$'" in script
    # Read-back resolves by the explicit SAM (which differs from the CN), not the name.
    assert "Get-ADComputer -Identity 'SQLALT$'" in script
    assert result["identity"] == "SQLALT$"


async def test_computer_unjoin_disables_recoverably() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_computer_unjoin(connector, _TARGET, {"identity": "SQLNODE1"})
    script = _script_from_call(run_mock)
    assert "Disable-ADAccount -Identity 'SQLNODE1'" in script
    assert "Remove-ADComputer" not in script  # unjoin is recoverable, not a delete
    assert result == {"identity": "SQLNODE1", "action": "unjoin", "op_class": "write"}


async def test_computer_delete_builds_remove_with_confirm() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_computer_delete(connector, _TARGET, {"identity": "SQLNODE1$"})
    script = _script_from_call(run_mock)
    assert "Remove-ADComputer -Identity 'SQLNODE1$' -Confirm:$false" in script
    assert result["action"] == "delete"


# ---------------------------------------------------------------------------
# ou group
# ---------------------------------------------------------------------------


async def test_ou_list_builds_envelope() -> None:
    connector = MsadConnector()
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_ou_list(connector, _TARGET, {})
    assert "Get-ADOrganizationalUnit -Filter *" in _script_from_call(run_mock)
    assert result == {"rows": [], "total": 0}


async def test_ou_create_builds_and_renders_protected() -> None:
    connector = MsadConnector()
    run_mock = _run('{"Name":"Servers","DistinguishedName":"OU=Servers,DC=c1sql,DC=lab"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_ou_create(
            connector,
            _TARGET,
            {"name": "Serv'ers", "path": "DC=c1sql,DC=lab", "protected": False},
        )
    script = _script_from_call(run_mock)
    assert "New-ADOrganizationalUnit -Name 'Serv''ers'" in script
    assert "-Path 'DC=c1sql,DC=lab'" in script
    assert "-ProtectedFromAccidentalDeletion $false" in script
    assert result["action"] == "create"


async def test_ou_move_builds_move_adobject() -> None:
    connector = MsadConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await msad_ou_move(
            connector,
            _TARGET,
            {
                "identity": "CN=svc-sql,DC=c1sql,DC=lab",
                "target_path": "OU=Servers,DC=c1sql,DC=lab",
            },
        )
    script = _script_from_call(run_mock)
    assert "Move-ADObject -Identity 'CN=svc-sql,DC=c1sql,DC=lab'" in script
    assert "-TargetPath 'OU=Servers,DC=c1sql,DC=lab'" in script
    assert result == {
        "identity": "CN=svc-sql,DC=c1sql,DC=lab",
        "target_path": "OU=Servers,DC=c1sql,DC=lab",
        "action": "move",
        "op_class": "write",
    }


# ---------------------------------------------------------------------------
# about (fingerprint wrapper) + probe
# ---------------------------------------------------------------------------


async def test_about_maps_fingerprint_into_identity_dict() -> None:
    connector = MsadConnector()
    payload = (
        '{"DNSRoot":"c1sql.lab","NetBIOSName":"C1SQL","Forest":"c1sql.lab",'
        '"DomainMode":"Windows2016Domain","PDCEmulator":"dc1.c1sql.lab",'
        '"ADModuleVersion":"1.0.1.0"}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "microsoft"
    assert result["product"] == "active-directory"
    assert result["version"] == "Windows2016Domain"
    assert result["dns_root"] == "c1sql.lab"
    assert result["netbios_name"] == "C1SQL"
    assert result["forest"] == "c1sql.lab"
    assert result["pdc_emulator"] == "dc1.c1sql.lab"
    assert result["ad_module_version"] == "1.0.1.0"


async def test_fingerprint_reads_get_addomain() -> None:
    connector = MsadConnector()
    run_mock = _run('{"DNSRoot":"c1sql.lab","DomainMode":"Windows2016Domain"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.fingerprint(_TARGET)
    assert "Get-ADDomain" in _script_from_call(run_mock)
    assert result.reachable is True
    assert result.product == "active-directory"
    assert result.version == "Windows2016Domain"


async def test_fingerprint_unreachable_is_not_an_exception() -> None:
    connector = MsadConnector()
    with patch.object(connector, "_run_command", AsyncMock(side_effect=OSError("down"))):
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is False
    assert result.vendor == "microsoft"
    assert "error" in result.extras


async def test_probe_tcp_unreachable_and_auth_failed() -> None:
    connector = MsadConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("no route"))):
        assert (await connector.probe(_TARGET)).reason == "tcp_unreachable"
    with patch.object(
        connector, "_connect", AsyncMock(side_effect=asyncssh.PermissionDenied("no"))
    ):
        assert (await connector.probe(_TARGET)).reason == "ssh_auth_failed"


async def test_probe_powershell_unavailable_when_pwsh_errors() -> None:
    connector = MsadConnector()
    from meho_backplane.connectors._shared.pwsh import PwshRunError

    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(return_value=_completed_process(stdout="", exit_status=1)),
        ),
    ):
        result = await connector.probe(_TARGET)
    assert result.reason == "powershell_unavailable"
    assert isinstance(PwshRunError("x", exit_status=1, stderr=""), Exception)  # import guard


@pytest.mark.parametrize(
    "boom",
    [OSError("reset"), asyncssh.ConnectionLost("closed"), TimeoutError("timeout")],
)
async def test_probe_command_failed_after_connect(boom: Exception) -> None:
    connector = MsadConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", AsyncMock(side_effect=boom)),
    ):
        result = await connector.probe(_TARGET)
    assert result.reason == "command_failed"


async def test_probe_ad_module_unavailable() -> None:
    connector = MsadConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"ad_module":false,"domain":false}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "ad_module_unavailable"


async def test_probe_domain_unreachable() -> None:
    connector = MsadConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"ad_module":true,"domain":false}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "domain_unreachable"


async def test_probe_ok_when_module_and_domain_present() -> None:
    connector = MsadConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"ad_module":true,"domain":true}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None

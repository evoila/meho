# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the winsrv connector (SSH → PowerShell / Windows Server core).

Mirrors the windows_dns harness (``test_connectors_windows_dns.py``): a
``_StubTarget`` dataclass, a ``_completed_process`` SSHCompletedProcess stub,
and ``patch.object(connector, "_run_command", AsyncMock(...))`` so the cmdlet
handlers can be exercised without a real Windows host. Because the transport
is ``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1),
the assertions decode the base64 payload back to the script and assert on the
cmdlet + quoted arguments the handler built.

There is no spec-reconcile lane — the cmdlet surface ships no OpenAPI (the
confirmed convention for SSH-typed connectors). Drift protection is this
ordinary unit suite. Coverage spans every op group, the injection-safety
escape on every parameterized script, and the secret-hygiene invariant that
no plaintext password ever enters a script.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.winsrv  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.registry import product_impl_id_round_trips
from meho_backplane.connectors.winsrv import WINSRV_OPS, WinsrvConnector
from meho_backplane.connectors.winsrv.ops import WINSRV_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.winsrv.ops_features import (
    winsrv_feature_install,
    winsrv_feature_list,
    winsrv_feature_remove,
)
from meho_backplane.connectors.winsrv.ops_localusers import (
    winsrv_localuser_create,
    winsrv_localuser_delete,
    winsrv_localuser_list,
    winsrv_localuser_set,
)
from meho_backplane.connectors.winsrv.ops_power import (
    winsrv_power_reboot,
    winsrv_power_shutdown,
)
from meho_backplane.connectors.winsrv.ops_services import (
    winsrv_service_get,
    winsrv_service_list,
    winsrv_service_restart,
    winsrv_service_start,
    winsrv_service_stop,
)
from meho_backplane.connectors.winsrv.ops_storage import (
    winsrv_disk_format,
    winsrv_disk_list,
    winsrv_iscsi_connect,
    winsrv_iscsi_list,
    winsrv_volume_list,
)
from meho_backplane.connectors.winsrv.ops_system import (
    winsrv_os_info,
    winsrv_pending_reboot,
    winsrv_uptime,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires (mirrors the windns suite)."""
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
    name="winsrv-test",
    host="win.test.invalid",
    port=22,
    secret_ref="meho/testing/winsrv/winsrv-test",
)


def _completed_process(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's :class:`SSHCompletedProcess`."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _script_from_call(run_mock: AsyncMock) -> str:
    """Recover the PowerShell script from the mocked ``_run_command`` argv.

    ``pwsh_run`` builds ``powershell -NoProfile -NonInteractive
    -EncodedCommand <base64-utf16le>`` and passes it as the 2nd positional arg
    to ``_run_command``. Decode the base64 tail back to the script text (which
    carries the transport's prepended ``$ProgressPreference`` guard).
    """
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
    connector = WinsrvConnector()
    assert connector.POWERSHELL_EXECUTABLE == "powershell"
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await winsrv_service_list(connector, _TARGET, {})
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert not cmd.startswith("pwsh ")
    assert _script_from_call(run_mock).startswith("$ProgressPreference = 'SilentlyContinue';")


# ---------------------------------------------------------------------------
# Registration / metadata invariants
# ---------------------------------------------------------------------------


def test_connector_id_triple_round_trips() -> None:
    assert WinsrvConnector.product == "winsrv"
    assert WinsrvConnector.version == "2022.x"
    assert WinsrvConnector.impl_id == "winsrv-ssh"
    assert product_impl_id_round_trips(product="winsrv", version="2022.x", impl_id="winsrv-ssh")


def test_op_surface_ids_and_safety_tiers() -> None:
    by_id = {op.op_id: op for op in WINSRV_OPS}
    assert len(by_id) == 23
    safe = {k for k, v in by_id.items() if v.safety_level == "safe"}
    caution = {k for k, v in by_id.items() if v.safety_level == "caution"}
    dangerous = {k for k, v in by_id.items() if v.safety_level == "dangerous"}
    assert dangerous == {
        "winsrv.power.reboot",
        "winsrv.power.shutdown",
        "winsrv.localuser.delete",
    }
    # Every dangerous op requires approval; nothing else does.
    assert {k for k, v in by_id.items() if v.requires_approval} == dangerous
    assert "winsrv.feature.install" in caution
    assert "winsrv.storage.disk.format" in caution
    assert {"winsrv.about", "winsrv.service.list", "winsrv.storage.disk.list"} <= safe


def test_every_group_has_a_curated_when_to_use() -> None:
    groups = {op.group_key for op in WINSRV_OPS}
    assert groups == {"system", "services", "features", "power", "localusers", "storage"}
    assert groups <= set(WINSRV_WHEN_TO_USE_BY_GROUP)


def test_every_handler_attr_resolves_on_the_class() -> None:
    for op in WINSRV_OPS:
        assert getattr(WinsrvConnector, op.handler_attr, None) is not None, op.op_id


# ---------------------------------------------------------------------------
# system group
# ---------------------------------------------------------------------------


async def test_os_info_builds_cim_query() -> None:
    connector = WinsrvConnector()
    payload = '{"Caption":"Microsoft Windows Server 2022 Datacenter","Version":"10.0.20348"}'
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_os_info(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-CimInstance -ClassName Win32_OperatingSystem" in script
    assert result["Version"] == "10.0.20348"


async def test_uptime_reads_last_boot_up_time() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"LastBootUpTime":"2026-09-01T00:00:00.0000000+00:00","UptimeSeconds":3600}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_uptime(connector, _TARGET, {})
    assert "LastBootUpTime" in _script_from_call(run_mock)
    assert result["UptimeSeconds"] == 3600


async def test_pending_reboot_probes_markers() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"pending_reboot":false,"component_based_servicing":false}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_pending_reboot(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Component Based Servicing" in script
    assert "PendingFileRenameOperations" in script
    assert result["pending_reboot"] is False


# ---------------------------------------------------------------------------
# services group
# ---------------------------------------------------------------------------


async def test_service_list_builds_envelope() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"rows":[{"Name":"MSSQLSERVER","Status":"Running"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_service_list(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-Service" in script
    assert "rows = $svc; total = $svc.Count" in script
    assert result["total"] == 1


@pytest.mark.parametrize("rows_json", ["[]", "null"])
async def test_service_list_empty_host(rows_json: str) -> None:
    connector = WinsrvConnector()
    run_mock = _run(f'{{"rows":{rows_json},"total":0}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_service_list(connector, _TARGET, {})
    assert result == {"rows": [], "total": 0}


async def test_service_get_builds_named_query_and_escapes() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"rows":[{"Name":"o\'db"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        await winsrv_service_get(connector, _TARGET, {"name": "o'db"})
    script = _script_from_call(run_mock)
    assert "-Name 'o''db'" in script  # single quote doubled inside the literal


@pytest.mark.parametrize(
    ("handler", "cmdlet", "action"),
    [
        (winsrv_service_start, "Start-Service", "start"),
        (winsrv_service_stop, "Stop-Service", "stop"),
        (winsrv_service_restart, "Restart-Service", "restart"),
    ],
)
async def test_service_actions_build_cmdlet(handler: Any, cmdlet: str, action: str) -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"name":"W32Time","status":"Running"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(connector, _TARGET, {"name": "W32Time"})
    script = _script_from_call(run_mock)
    assert f"{cmdlet} -Name 'W32Time'" in script
    assert result == {
        "name": "W32Time",
        "action": action,
        "status": "Running",
        "op_class": "write",
    }


# ---------------------------------------------------------------------------
# features group
# ---------------------------------------------------------------------------


async def test_feature_install_builds_cmdlet_and_toggles() -> None:
    connector = WinsrvConnector()
    run_mock = _run(
        '{"success":true,"exit_code":"Success","restart_needed":true,'
        '"features_changed":["Failover-Clustering"]}'
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_feature_install(
            connector,
            _TARGET,
            {"name": "Failover-Clustering", "include_management_tools": True},
        )
    script = _script_from_call(run_mock)
    assert "Install-WindowsFeature -Name 'Failover-Clustering'" in script
    assert "-IncludeManagementTools:$true" in script
    assert "-IncludeAllSubFeature:$false" in script
    assert "-Restart" not in script  # never auto-restart
    assert result["success"] is True
    assert result["restart_needed"] is True
    assert result["features_changed"] == ["Failover-Clustering"]


async def test_feature_remove_builds_uninstall() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"success":true,"exit_code":"Success","restart_needed":false}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_feature_remove(
            connector, _TARGET, {"name": "Web-Server", "include_management_tools": True}
        )
    script = _script_from_call(run_mock)
    assert "Uninstall-WindowsFeature -Name 'Web-Server'" in script
    # The management-tools toggle wires to -IncludeManagementTools (name matches wiring).
    assert "-IncludeManagementTools:$true" in script
    assert "-Restart" not in script
    assert result["action"] == "remove"


def test_feature_remove_toggle_param_name_matches_wiring() -> None:
    """Guard against the misleading-param-name regression: the remove op's
    management-tools toggle is named ``include_management_tools`` (matching the
    install op + the ``-IncludeManagementTools`` it drives), not
    ``include_all_sub_feature``."""
    by_id = {op.op_id: op for op in WINSRV_OPS}
    props = by_id["winsrv.feature.remove"].parameter_schema["properties"]
    assert "include_management_tools" in props
    assert "include_all_sub_feature" not in props


async def test_feature_list_builds_envelope() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"rows":[{"Name":"Web-Server","Installed":false}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_feature_list(connector, _TARGET, {})
    assert "Get-WindowsFeature" in _script_from_call(run_mock)
    assert result["total"] == 1


# ---------------------------------------------------------------------------
# power group (dangerous + requires_approval)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "flag", "action"),
    [(winsrv_power_reboot, "/r", "reboot"), (winsrv_power_shutdown, "/s", "shutdown")],
)
async def test_power_schedules_shutdown_exe(handler: Any, flag: str, action: str) -> None:
    connector = WinsrvConnector()
    run_mock = _run(f'{{"ok":true,"action":"{action}","delay_seconds":30}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(
            connector, _TARGET, {"delay_seconds": 30, "message": "patch o'clock"}
        )
    script = _script_from_call(run_mock)
    assert f"shutdown.exe {flag} /t 30" in script
    assert "/c 'patch o''clock'" in script  # message single-quote-escaped
    assert "$LASTEXITCODE -ne 0" in script  # native-exit guard
    assert result == {
        "ok": True,
        "action": action,
        "delay_seconds": 30,
        "message": "patch o'clock",
        "op_class": "write",
    }


async def test_power_default_delay_and_no_message() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"action":"reboot","delay_seconds":15}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_power_reboot(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "shutdown.exe /r /t 15" in script
    assert "/c" not in script
    assert result["delay_seconds"] == 15


async def test_power_rejects_negative_delay() -> None:
    connector = WinsrvConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await winsrv_power_reboot(connector, _TARGET, {"delay_seconds": -5})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# localusers group — incl. the secret-hygiene invariant
# ---------------------------------------------------------------------------


async def test_localuser_create_is_passwordless() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"user":{"Name":"svc-sql","Enabled":true}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_localuser_create(
            connector, _TARGET, {"name": "svc-sql", "description": "SQL service"}
        )
    script = _script_from_call(run_mock)
    assert "New-LocalUser -Name 'svc-sql' -NoPassword" in script
    assert "-Description 'SQL service'" in script
    # Secret-hygiene: no password material ever enters the script.
    assert "-Password " not in script
    assert "ConvertTo-SecureString" not in script
    assert result["action"] == "create"


async def test_localuser_set_builds_and_toggles() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"user":{"Name":"svc-sql","Enabled":false}}')
    with patch.object(connector, "_run_command", run_mock):
        await winsrv_localuser_set(
            connector, _TARGET, {"name": "svc-sql", "description": "x", "enabled": False}
        )
    script = _script_from_call(run_mock)
    assert "Set-LocalUser -Name 'svc-sql' -Description 'x'" in script
    assert "Disable-LocalUser -Name 'svc-sql'" in script
    assert "-Password " not in script


async def test_localuser_set_requires_an_attribute() -> None:
    connector = WinsrvConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await winsrv_localuser_set(connector, _TARGET, {"name": "svc-sql"})
    run_mock.assert_not_awaited()


async def test_localuser_delete_builds_remove() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_localuser_delete(connector, _TARGET, {"name": "svc-sql"})
    assert "Remove-LocalUser -Name 'svc-sql'" in _script_from_call(run_mock)
    assert result == {"name": "svc-sql", "action": "delete", "op_class": "write"}


async def test_localuser_list_reads_no_secret() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"rows":[{"Name":"Administrator"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_localuser_list(connector, _TARGET, {})
    assert "Get-LocalUser" in _script_from_call(run_mock)
    assert result["total"] == 1


def test_no_write_op_exposes_a_secret_value_field() -> None:
    """The secret-leak guard at the schema level: no op accepts a plaintext
    secret-value parameter (password / secret / chap credential), so a secret
    can never reach the ``-EncodedCommand`` argv. Boolean policy flags whose
    NAME contains 'password' (e.g. ``password_never_expires``) are fine — the
    guard keys on exact secret-value field names, not substrings."""
    secret_fields = {"password", "secret", "chap_secret", "chapsecret", "chap_username"}
    for op in WINSRV_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


# ---------------------------------------------------------------------------
# storage group
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "cmdlet"),
    [
        (winsrv_disk_list, "Get-Disk"),
        (winsrv_volume_list, "Get-Volume"),
        (winsrv_iscsi_list, "Get-IscsiTarget"),
    ],
)
async def test_storage_lists_build_envelope(handler: Any, cmdlet: str) -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(connector, _TARGET, {})
    assert cmdlet in _script_from_call(run_mock)
    assert result == {"rows": [], "total": 0}


async def test_iscsi_connect_builds_cmdlet_and_escapes() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"is_connected":true,"is_persistent":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_iscsi_connect(
            connector,
            _TARGET,
            {"node_address": "iqn.1991-05.com.microsoft:t'gt", "target_portal_address": "10.0.0.5"},
        )
    script = _script_from_call(run_mock)
    assert "Connect-IscsiTarget -NodeAddress 'iqn.1991-05.com.microsoft:t''gt'" in script
    assert "-TargetPortalAddress '10.0.0.5'" in script
    assert "-IsPersistent $true" in script
    assert result["is_connected"] is True


async def test_iscsi_connect_rejects_bad_port() -> None:
    connector = WinsrvConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await winsrv_iscsi_connect(
            connector, _TARGET, {"node_address": "iqn.x", "target_portal_port": 99999}
        )
    run_mock.assert_not_awaited()


async def test_disk_format_provisions_raw_disk() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"disk":3,"drive_letter":"E","filesystem":"NTFS"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_disk_format(
            connector, _TARGET, {"disk_number": 3, "drive_letter": "e", "label": "sql'data"}
        )
    script = _script_from_call(run_mock)
    assert "Get-Disk -Number 3" in script
    assert "Initialize-Disk -Number 3 -PartitionStyle GPT" in script
    assert "New-Partition -DiskNumber 3 -UseMaximumSize -DriveLetter E" in script
    assert "-FileSystem 'NTFS'" in script
    assert "-NewFileSystemLabel 'sql''data'" in script
    assert "-not $false" in script  # force defaults to $false → non-RAW disk refused
    # The RAW-guard throw uses an unambiguous double-quoted subexpression.
    assert 'throw "disk 3 is not RAW (PartitionStyle=$($d.PartitionStyle))' in script
    assert result["drive_letter"] == "E"


async def test_disk_format_force_flag_rendered() -> None:
    connector = WinsrvConnector()
    run_mock = _run('{"ok":true,"disk":3,"drive_letter":"E","filesystem":"NTFS"}')
    with patch.object(connector, "_run_command", run_mock):
        await winsrv_disk_format(connector, _TARGET, {"disk_number": 3, "force": True})
    script = _script_from_call(run_mock)
    assert "-not $true" in script
    assert "-AssignDriveLetter" in script  # no drive_letter → auto-assign


@pytest.mark.parametrize(
    "params",
    [
        {"disk_number": 3, "filesystem": "ext4"},
        {"disk_number": 3, "drive_letter": "EE"},
        {"disk_number": 3, "drive_letter": "Ä"},  # unicode letter — ASCII-only per the schema
        {"disk_number": 3, "drive_letter": "5"},
        {"disk_number": -1},
    ],
)
async def test_disk_format_rejects_bad_input(params: dict[str, Any]) -> None:
    connector = WinsrvConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await winsrv_disk_format(connector, _TARGET, params)
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# about (fingerprint wrapper) + probe
# ---------------------------------------------------------------------------


async def test_about_maps_fingerprint_into_identity_dict() -> None:
    connector = WinsrvConnector()
    payload = (
        '{"Hostname":"SQL01","Caption":"Microsoft Windows Server 2022 Datacenter",'
        '"Version":"10.0.20348","BuildNumber":"20348","PowerShellVersion":"5.1.20348.1"}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "microsoft"
    assert result["product"] == "windows-server"
    assert result["version"] == "10.0.20348"
    assert result["build"] == "20348"
    assert result["hostname"] == "SQL01"
    assert result["os_caption"].startswith("Microsoft Windows Server 2022")
    assert result["powershell_version"] == "5.1.20348.1"


@pytest.mark.parametrize(
    "boom",
    [
        OSError("connection reset by peer"),
        asyncssh.ConnectionLost("channel closed mid-command"),
        TimeoutError("probe script timed out"),
    ],
)
async def test_probe_command_failed_when_run_command_raises_after_connect(boom: Exception) -> None:
    connector = WinsrvConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", AsyncMock(side_effect=boom)),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "command_failed"


async def test_probe_os_query_failed_when_cim_unreadable() -> None:
    connector = WinsrvConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"present":false}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "os_query_failed"


async def test_probe_ok_when_cim_readable() -> None:
    connector = WinsrvConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"present":true}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None


async def test_probe_tcp_unreachable_and_auth_failed() -> None:
    connector = WinsrvConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("no route"))):
        assert (await connector.probe(_TARGET)).reason == "tcp_unreachable"
    with patch.object(
        connector, "_connect", AsyncMock(side_effect=asyncssh.PermissionDenied("no"))
    ):
        assert (await connector.probe(_TARGET)).reason == "ssh_auth_failed"


async def test_fingerprint_unreachable_is_not_an_exception() -> None:
    connector = WinsrvConnector()
    with patch.object(connector, "_run_command", AsyncMock(side_effect=OSError("down"))):
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is False
    assert result.vendor == "microsoft"
    assert "error" in result.extras

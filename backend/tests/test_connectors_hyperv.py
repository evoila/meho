# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the hyperv connector (SSH → PowerShell / Hyper-V migration source).

Mirrors the winsrv harness (``test_connectors_winsrv.py``): a ``_StubTarget``
dataclass, a ``_completed_process`` SSHCompletedProcess stub, and
``patch.object(connector, "_run_command", AsyncMock(...))`` so the cmdlet
handlers can be exercised without a real Hyper-V host. Because the transport is
``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1), the
assertions decode the base64 payload back to the script and assert on the cmdlet
+ quoted arguments the handler built.

There is no spec-reconcile lane — the cmdlet surface ships no OpenAPI (the
confirmed convention for SSH-typed connectors). Drift protection is this
ordinary unit suite. Coverage spans every op group, the injection-safety escape
on every parameterized script, the ``{rows, total}`` JSONFlux envelope on the
list ops, and the secret-hygiene invariant that no op accepts a secret-value
parameter.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.hyperv  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.hyperv import HYPERV_OPS, HypervConnector
from meho_backplane.connectors.hyperv.ops import HYPERV_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.hyperv.ops_checkpoints import (
    hyperv_checkpoints_create,
    hyperv_checkpoints_delete,
    hyperv_checkpoints_list,
    hyperv_checkpoints_revert,
)
from meho_backplane.connectors.hyperv.ops_disks import (
    hyperv_disks_vhd_chain,
    hyperv_disks_vhd_get,
    hyperv_disks_vm_list,
)
from meho_backplane.connectors.hyperv.ops_export import hyperv_export_vm
from meho_backplane.connectors.hyperv.ops_host import (
    hyperv_host_info,
    hyperv_host_numa,
    hyperv_host_vswitch_list,
)
from meho_backplane.connectors.hyperv.ops_power import (
    hyperv_power_start,
    hyperv_power_stop,
)
from meho_backplane.connectors.hyperv.ops_vms import (
    hyperv_vms_config,
    hyperv_vms_get,
    hyperv_vms_list,
    hyperv_vms_state,
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
    name="hyperv-test",
    host="hv.test.invalid",
    port=22,
    secret_ref="meho/testing/hyperv/hyperv-test",
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

    ``pwsh_run`` builds ``powershell -NoProfile -NonInteractive -EncodedCommand
    <base64-utf16le>`` and passes it as the 2nd positional arg to
    ``_run_command``. Decode the base64 tail back to the script text (which
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
    connector = HypervConnector()
    assert connector.POWERSHELL_EXECUTABLE == "powershell"
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await hyperv_vms_list(connector, _TARGET, {})
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert not cmd.startswith("pwsh ")
    assert _script_from_call(run_mock).startswith("$ProgressPreference = 'SilentlyContinue';")


# ---------------------------------------------------------------------------
# Registration / metadata invariants
# ---------------------------------------------------------------------------


def test_connector_id_triple_round_trips() -> None:
    assert HypervConnector.product == "hyperv"
    assert HypervConnector.version == "2022.x"
    assert HypervConnector.impl_id == "hyperv-ssh"
    # The product token is separator-free so the connector_id round-trips
    # (a `hyper-v` token would break the registry's round-trip guard at boot).
    assert product_impl_id_round_trips(product="hyperv", version="2022.x", impl_id="hyperv-ssh")


def test_op_surface_ids_and_safety_tiers() -> None:
    by_id = {op.op_id: op for op in HYPERV_OPS}
    assert len(by_id) == 18
    safe = {k for k, v in by_id.items() if v.safety_level == "safe"}
    caution = {k for k, v in by_id.items() if v.safety_level == "caution"}
    dangerous = {k for k, v in by_id.items() if v.safety_level == "dangerous"}
    # Reverting or deleting a checkpoint is the only destructive surface.
    assert dangerous == {"hyperv.checkpoints.revert", "hyperv.checkpoints.delete"}
    # Every dangerous op requires approval; nothing else does.
    assert {k for k, v in by_id.items() if v.requires_approval} == dangerous
    # Recoverable writes are caution (the migration-seed + cutover verbs).
    assert caution == {
        "hyperv.checkpoints.create",
        "hyperv.export.vm",
        "hyperv.power.start",
        "hyperv.power.stop",
    }
    # The whole assessment surface (host / vms / disks / checkpoint-list) is safe.
    assert {
        "hyperv.about",
        "hyperv.host.info",
        "hyperv.vms.list",
        "hyperv.disks.vhd.get",
        "hyperv.checkpoints.list",
    } <= safe


def test_every_group_has_a_curated_when_to_use() -> None:
    groups = {op.group_key for op in HYPERV_OPS}
    assert groups == {"host", "vms", "disks", "checkpoints", "export", "power"}
    assert groups <= set(HYPERV_WHEN_TO_USE_BY_GROUP)


def test_every_handler_attr_resolves_on_the_class() -> None:
    for op in HYPERV_OPS:
        assert getattr(HypervConnector, op.handler_attr, None) is not None, op.op_id


def test_no_op_exposes_a_secret_value_field() -> None:
    """The secret-leak guard at the schema level: no op accepts a plaintext
    secret-value parameter, so a secret can never reach the ``-EncodedCommand``
    argv. The SSH credential is the only secret and it never enters a script."""
    secret_fields = {"password", "secret", "credential", "chap_secret", "chapsecret"}
    for op in HYPERV_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


# ---------------------------------------------------------------------------
# host group
# ---------------------------------------------------------------------------


async def test_host_info_reads_vmhost() -> None:
    connector = HypervConnector()
    payload = (
        '{"LogicalProcessorCount":32,"MemoryCapacity":137438953472,"NumaSpanningEnabled":true}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_host_info(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-VMHost" in script
    assert "LogicalProcessorCount" in script
    assert result["LogicalProcessorCount"] == 32


async def test_host_numa_builds_envelope() -> None:
    connector = HypervConnector()
    run_mock = _run('{"rows":[{"NodeId":0,"MemoryTotal":68719476736}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_host_numa(connector, _TARGET, {})
    assert "Get-VMHostNumaNode" in _script_from_call(run_mock)
    assert result == {"rows": [{"NodeId": 0, "MemoryTotal": 68719476736}], "total": 1}


@pytest.mark.parametrize("rows_json", ["[]", "null"])
async def test_vswitch_list_empty_host(rows_json: str) -> None:
    connector = HypervConnector()
    run_mock = _run(f'{{"rows":{rows_json},"total":0}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_host_vswitch_list(connector, _TARGET, {})
    assert "Get-VMSwitch" in _script_from_call(run_mock)
    assert result == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# vms group (the migration-assessment surface)
# ---------------------------------------------------------------------------


async def test_vms_list_builds_envelope_and_stringifies_state() -> None:
    connector = HypervConnector()
    run_mock = _run('{"rows":[{"Name":"sql-01","State":"Running","Generation":2}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_vms_list(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-VM | Select-Object" in script
    # State / Id / Version enum+compound fields are stringified for a readable
    # assessment surface (PS 5.1 ConvertTo-Json renders enums as integers raw).
    assert "@{N='State';E={\"$($_.State)\"}}" in script
    assert "IntegrationServicesVersion" in script
    assert result["total"] == 1


async def test_vms_get_escapes_name() -> None:
    connector = HypervConnector()
    run_mock = _run('{"rows":[{"Name":"o\'db"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_vms_get(connector, _TARGET, {"vm_name": "o'db"})
    script = _script_from_call(run_mock)
    assert "$n = 'o''db'" in script  # single quote doubled inside the literal
    assert "Get-VM -Name $n" in script
    assert result["vm_name"] == "o'db"


async def test_vms_config_guards_firmware_by_generation() -> None:
    connector = HypervConnector()
    run_mock = _run('{"Name":"sql-01","Generation":2,"SecureBoot":"On","ProcessorCount":4}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_vms_config(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "$n = 'sql-01'" in script
    assert "if ($vm.Generation -eq 2)" in script  # firmware read guarded to Gen 2
    assert "Get-VMFirmware -VMName $n" in script
    assert "MemoryStartup = $vm.MemoryStartup" in script
    assert result["SecureBoot"] == "On"


async def test_vms_state_reads_runtime() -> None:
    connector = HypervConnector()
    run_mock = _run('{"Name":"sql-01","State":"Running","Heartbeat":"OkApplicationsHealthy"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_vms_state(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "$vm = Get-VM -Name $n" in script
    assert "Heartbeat = if ($vm.Heartbeat)" in script
    assert result["State"] == "Running"


# ---------------------------------------------------------------------------
# disks group (the VHDX→VMDK planning input)
# ---------------------------------------------------------------------------


async def test_disks_vm_list_builds_envelope() -> None:
    connector = HypervConnector()
    run_mock = _run('{"rows":[{"Path":"C:\\\\vm\\\\d.vhdx","ControllerType":"SCSI"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_disks_vm_list(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "Get-VMHardDiskDrive -VMName $n" in script
    assert result["vm_name"] == "sql-01"
    assert result["total"] == 1


async def test_disks_vhd_get_escapes_path_and_reads_facts() -> None:
    connector = HypervConnector()
    run_mock = _run('{"Path":"C:\\\\vm\\\\d.vhdx","VhdFormat":"VHDX","VhdType":"Dynamic"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_disks_vhd_get(connector, _TARGET, {"path": "C:\\vm\\o'db.vhdx"})
    script = _script_from_call(run_mock)
    assert "Get-VHD -Path 'C:\\vm\\o''db.vhdx'" in script  # single quote doubled
    assert 'VhdFormat = "$($v.VhdFormat)"' in script
    assert "FragmentationPercentage = $v.FragmentationPercentage" in script
    assert result["VhdFormat"] == "VHDX"


async def test_disks_vhd_chain_walks_parent_and_normalises() -> None:
    connector = HypervConnector()
    payload = (
        '{"rows":[{"Path":"leaf.avhdx","ParentPath":"base.vhdx"},'
        '{"Path":"base.vhdx","ParentPath":null}],"total":2}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_disks_vhd_chain(connector, _TARGET, {"path": "leaf.avhdx"})
    script = _script_from_call(run_mock)
    assert "$p = 'leaf.avhdx'" in script
    assert "while ($p) {" in script
    assert "$p = $v.ParentPath" in script  # follows the parent link
    assert result["path"] == "leaf.avhdx"
    assert result["total"] == 2


async def test_disks_vhd_chain_single_disk_collapses_to_one_row() -> None:
    connector = HypervConnector()
    # PS ConvertTo-Json can collapse a single-element array to a bare object;
    # normalise_json_rows re-wraps it so total stays honest.
    run_mock = _run('{"rows":{"Path":"base.vhdx","ParentPath":null},"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_disks_vhd_chain(connector, _TARGET, {"path": "base.vhdx"})
    assert result["total"] == 1
    assert result["rows"] == [{"Path": "base.vhdx", "ParentPath": None}]


# ---------------------------------------------------------------------------
# checkpoints group (list safe / create caution / revert+delete dangerous)
# ---------------------------------------------------------------------------


async def test_checkpoints_list_builds_envelope() -> None:
    connector = HypervConnector()
    run_mock = _run('{"rows":[{"Name":"pre-cutover","SnapshotType":"Standard"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_checkpoints_list(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "Get-VMSnapshot -VMName $n" in script
    assert result["vm_name"] == "sql-01"
    assert result["total"] == 1


async def test_checkpoints_create_named_and_passthru() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true,"vm":"sql-01","checkpoint":"pre-cutover"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_checkpoints_create(
            connector, _TARGET, {"vm_name": "sql-01", "checkpoint_name": "pre'cut"}
        )
    script = _script_from_call(run_mock)
    assert "Checkpoint-VM -Name $n -SnapshotName 'pre''cut' -Passthru" in script
    assert result == {
        "vm_name": "sql-01",
        "checkpoint_name": "pre-cutover",
        "action": "create",
        "op_class": "write",
    }


async def test_checkpoints_create_auto_named_when_omitted() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true,"vm":"sql-01","checkpoint":"sql-01 - (2026)"}')
    with patch.object(connector, "_run_command", run_mock):
        await hyperv_checkpoints_create(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "Checkpoint-VM -Name $n -Passthru" in script
    assert "-SnapshotName" not in script  # omitted → Hyper-V auto-names it


@pytest.mark.parametrize(
    ("handler", "cmdlet", "action"),
    [
        (hyperv_checkpoints_revert, "Restore-VMSnapshot", "revert"),
        (hyperv_checkpoints_delete, "Remove-VMSnapshot", "delete"),
    ],
)
async def test_checkpoint_writes_build_confirm_false_and_escape(
    handler: Any, cmdlet: str, action: str
) -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(
            connector, _TARGET, {"vm_name": "sql-01", "checkpoint_name": "pre'cut"}
        )
    script = _script_from_call(run_mock)
    assert f"{cmdlet} -Name 'pre''cut' -VMName 'sql-01' -Confirm:$false" in script
    assert result == {
        "vm_name": "sql-01",
        "checkpoint_name": "pre'cut",
        "action": action,
        "op_class": "write",
    }


# ---------------------------------------------------------------------------
# export group (the migration seed; long-running)
# ---------------------------------------------------------------------------


async def test_export_vm_builds_cmdlet_and_forwards_timeout() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_export_vm(
            connector,
            _TARGET,
            {"vm_name": "sql-01", "path": "E:\\exports", "timeout_seconds": 7200},
        )
    script = _script_from_call(run_mock)
    assert "Export-VM -Name 'sql-01' -Path 'E:\\exports'" in script
    # The wall-clock budget is forwarded to the transport's per-call timeout.
    assert run_mock.await_args.kwargs["timeout"] == 7200.0
    assert result == {
        "vm_name": "sql-01",
        "path": "E:\\exports",
        "action": "export",
        "timeout_seconds": 7200,
        "op_class": "write",
    }


async def test_export_vm_default_timeout() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_export_vm(connector, _TARGET, {"vm_name": "vm", "path": "E:\\x"})
    assert run_mock.await_args.kwargs["timeout"] == 3600.0
    assert result["timeout_seconds"] == 3600


@pytest.mark.parametrize("bad", [0, -1, 999999, "3600", 3.5, True])
async def test_export_vm_rejects_bad_timeout(bad: Any) -> None:
    connector = HypervConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await hyperv_export_vm(
            connector, _TARGET, {"vm_name": "vm", "path": "E:\\x", "timeout_seconds": bad}
        )
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# power group (source-side cutover verbs)
# ---------------------------------------------------------------------------


async def test_power_start_builds_cmdlet() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true,"state":"Running"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_power_start(connector, _TARGET, {"vm_name": "sql-01"})
    script = _script_from_call(run_mock)
    assert "Start-VM -Name 'sql-01' -Passthru" in script
    assert result == {
        "vm_name": "sql-01",
        "action": "start",
        "state": "Running",
        "op_class": "write",
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("shutdown", "Stop-VM -Name 'sql-01' -Force -Passthru"),
        ("turnoff", "Stop-VM -Name 'sql-01' -TurnOff -Force -Passthru"),
        ("save", "Stop-VM -Name 'sql-01' -Save -Force -Passthru"),
    ],
)
async def test_power_stop_modes(mode: str, expected: str) -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true,"state":"Off"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_power_stop(connector, _TARGET, {"vm_name": "sql-01", "mode": mode})
    script = _script_from_call(run_mock)
    assert expected in script
    # A graceful stop can take minutes; a wider transport timeout is used.
    assert run_mock.await_args.kwargs["timeout"] == 360.0
    assert result["mode"] == mode
    assert result["action"] == "stop"


async def test_power_stop_defaults_to_graceful_shutdown() -> None:
    connector = HypervConnector()
    run_mock = _run('{"ok":true,"state":"Off"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_power_stop(connector, _TARGET, {"vm_name": "vm"})
    script = _script_from_call(run_mock)
    # Default (graceful) mode adds no mode switch of its own — no double -Force.
    assert "Stop-VM -Name 'vm' -Force -Force -Passthru" not in script
    assert "Stop-VM -Name 'vm' -Force -Passthru" in script
    assert result["mode"] == "shutdown"


async def test_power_stop_rejects_bad_mode() -> None:
    connector = HypervConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await hyperv_power_stop(connector, _TARGET, {"vm_name": "vm", "mode": "nuke"})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# about (fingerprint wrapper) + probe
# ---------------------------------------------------------------------------


async def test_about_maps_fingerprint_into_identity_dict() -> None:
    connector = HypervConnector()
    payload = (
        '{"Hostname":"HV01","Caption":"Microsoft Windows Server 2022 Datacenter",'
        '"OsVersion":"10.0.20348","BuildNumber":"20348","PowerShellVersion":"5.1.20348.1",'
        '"HyperVModule":true,"HyperVModuleVersion":"2.0.0.0","HypervisorPresent":true}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "microsoft"
    assert result["product"] == "hyper-v"
    assert result["version"] == "10.0.20348"
    assert result["build"] == "20348"
    assert result["hostname"] == "HV01"
    assert result["hyperv_module"] is True
    assert result["hyperv_module_version"] == "2.0.0.0"
    assert result["hypervisor_present"] is True


async def test_probe_ok_when_module_and_hypervisor_present() -> None:
    connector = HypervConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":true,"hypervisor":true}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None


async def test_probe_hyperv_module_absent() -> None:
    connector = HypervConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":false,"hypervisor":false}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "hyperv_module_absent"


async def test_probe_hypervisor_role_absent() -> None:
    connector = HypervConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":true,"hypervisor":false}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "hypervisor_role_absent"


@pytest.mark.parametrize(
    "boom",
    [
        OSError("connection reset by peer"),
        asyncssh.ConnectionLost("channel closed mid-command"),
        TimeoutError("probe script timed out"),
    ],
)
async def test_probe_command_failed_when_run_command_raises_after_connect(boom: Exception) -> None:
    connector = HypervConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", AsyncMock(side_effect=boom)),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "command_failed"


async def test_probe_tcp_unreachable_and_auth_failed() -> None:
    connector = HypervConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("no route"))):
        assert (await connector.probe(_TARGET)).reason == "tcp_unreachable"
    with patch.object(
        connector, "_connect", AsyncMock(side_effect=asyncssh.PermissionDenied("no"))
    ):
        assert (await connector.probe(_TARGET)).reason == "ssh_auth_failed"


async def test_probe_powershell_unavailable() -> None:
    from meho_backplane.connectors._shared.pwsh import PwshRunError

    connector = HypervConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(
            connector,
            "_run_command",
            AsyncMock(side_effect=PwshRunError("boom", exit_status=1, stderr="")),
        ),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "powershell_unavailable"


async def test_fingerprint_unreachable_is_not_an_exception() -> None:
    connector = HypervConnector()
    with patch.object(connector, "_run_command", AsyncMock(side_effect=OSError("down"))):
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is False
    assert result.vendor == "microsoft"
    assert result.product == "hyper-v"
    assert "error" in result.extras

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the wsfc connector (SSH → PowerShell / FailoverClusters).

Mirrors the winsrv harness (``test_connectors_winsrv.py``): a ``_StubTarget``
dataclass, a ``_completed_process`` SSHCompletedProcess stub, and
``patch.object(connector, "_run_command", AsyncMock(...))`` so the cmdlet
handlers can be exercised without a real cluster. Because the transport is
``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1), the
assertions decode the base64 payload back to the script and assert on the
cmdlet + quoted arguments the handler built — that decode is the injection-
safety check (an operator string must appear only inside a doubled
single-quoted literal).

There is no spec-reconcile lane — the cmdlet surface ships no OpenAPI (the
confirmed convention for SSH-typed connectors). Drift protection is this
ordinary unit suite.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

import meho_backplane.connectors.wsfc  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.registry import product_impl_id_round_trips
from meho_backplane.connectors.wsfc import WSFC_OPS, WsfcConnector
from meho_backplane.connectors.wsfc.ops import WSFC_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.wsfc.ops_cluster import (
    wsfc_cluster_get,
    wsfc_cluster_quorum,
    wsfc_cluster_test,
    wsfc_cluster_validation_report,
)
from meho_backplane.connectors.wsfc.ops_groups import (
    wsfc_group_list,
    wsfc_group_move,
    wsfc_group_offline,
    wsfc_group_online,
    wsfc_group_state,
)
from meho_backplane.connectors.wsfc.ops_nodes import (
    wsfc_node_evict,
    wsfc_node_list,
    wsfc_node_pause,
    wsfc_node_resume,
    wsfc_node_state,
)
from meho_backplane.connectors.wsfc.ops_resources import (
    wsfc_resource_dependency_report,
    wsfc_resource_list,
)
from meho_backplane.connectors.wsfc.ops_witness import wsfc_witness_get, wsfc_witness_set
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
    name="wsfc-test",
    host="node1.test.invalid",
    port=22,
    secret_ref="meho/testing/wsfc/wsfc-test",
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
    connector = WsfcConnector()
    assert connector.POWERSHELL_EXECUTABLE == "powershell"
    run_mock = _run('{"rows":[],"total":0}')
    with patch.object(connector, "_run_command", run_mock):
        await wsfc_node_list(connector, _TARGET, {})
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert not cmd.startswith("pwsh ")
    assert _script_from_call(run_mock).startswith("$ProgressPreference = 'SilentlyContinue';")


# ---------------------------------------------------------------------------
# Registration / metadata invariants
# ---------------------------------------------------------------------------


def test_connector_id_triple_round_trips() -> None:
    assert WsfcConnector.product == "wsfc"
    assert WsfcConnector.version == "2022.x"
    assert WsfcConnector.impl_id == "wsfc-ssh"
    assert product_impl_id_round_trips(product="wsfc", version="2022.x", impl_id="wsfc-ssh")


def test_op_surface_ids_and_safety_tiers() -> None:
    by_id = {op.op_id: op for op in WSFC_OPS}
    assert len(by_id) == 19
    safe = {k for k, v in by_id.items() if v.safety_level == "safe"}
    caution = {k for k, v in by_id.items() if v.safety_level == "caution"}
    dangerous = {k for k, v in by_id.items() if v.safety_level == "dangerous"}
    assert dangerous == {
        "wsfc.nodes.evict",
        "wsfc.groups.offline",
        "wsfc.groups.online",
    }
    # Every dangerous op requires approval; nothing else does.
    assert {k for k, v in by_id.items() if v.requires_approval} == dangerous
    assert caution == {
        "wsfc.cluster.test",
        "wsfc.nodes.pause",
        "wsfc.nodes.resume",
        "wsfc.groups.move",
        "wsfc.witness.set",
    }
    assert {
        "wsfc.about",
        "wsfc.cluster.get",
        "wsfc.cluster.quorum",
        "wsfc.cluster.validation-report",
        "wsfc.nodes.list",
        "wsfc.groups.state",
        "wsfc.resources.dependency-report",
        "wsfc.witness.get",
    } <= safe


def test_every_group_has_a_curated_when_to_use() -> None:
    groups = {op.group_key for op in WSFC_OPS}
    assert groups == {"cluster", "nodes", "groups", "resources", "witness"}
    assert groups <= set(WSFC_WHEN_TO_USE_BY_GROUP)


def test_every_handler_attr_resolves_on_the_class() -> None:
    for op in WSFC_OPS:
        assert getattr(WsfcConnector, op.handler_attr, None) is not None, op.op_id


# ---------------------------------------------------------------------------
# cluster group
# ---------------------------------------------------------------------------


async def test_cluster_get_builds_health_rollup() -> None:
    connector = WsfcConnector()
    payload = (
        '{"name":"c1sql1","nodes_total":2,"nodes_up":2,"nodes_down":0,'
        '"groups_total":3,"groups_online":3,"groups_failed":0,'
        '"resources_total":8,"resources_online":8,"resources_failed":0}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_cluster_get(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-Cluster" in script
    assert "Get-ClusterNode" in script
    assert "Get-ClusterGroup" in script
    assert "Get-ClusterResource" in script
    # Hashtable values are plain expressions (no inline-function calls), and the
    # per-state maps are precomputed variables.
    assert "nodes_up = @($nodes | Where-Object { \"$($_.State)\" -eq 'Up' }).Count" in script
    assert "nodes_by_state = $nbs" in script
    assert result["nodes_up"] == 2
    assert result["groups_failed"] == 0


async def test_cluster_quorum_builds_query() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"cluster":"c1sql1","quorum_type":"NodeAndFileShareMajority"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_cluster_quorum(connector, _TARGET, {})
    assert "Get-ClusterQuorum" in _script_from_call(run_mock)
    assert result["quorum_type"] == "NodeAndFileShareMajority"


@pytest.mark.parametrize("rows_json", ["[]", "null"])
async def test_validation_report_empty(rows_json: str) -> None:
    connector = WsfcConnector()
    run_mock = _run(f'{{"rows":{rows_json},"total":0}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_cluster_validation_report(connector, _TARGET, {})
    assert "Cluster\\Reports" in _script_from_call(run_mock)
    assert result == {"rows": [], "total": 0}


async def test_cluster_test_runs_with_raised_timeout_and_escapes_include() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"report_path":"C:\\\\x.mht"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_cluster_test(connector, _TARGET, {"include": ["Inventory", "Net'work"]})
    script = _script_from_call(run_mock)
    assert "Test-Cluster -Include 'Inventory', 'Net''work'" in script
    assert "-Confirm:$false" in script
    # LONG-running: the handler forwards a raised timeout to _run_command.
    assert run_mock.await_args.kwargs["timeout"] == 900.0
    assert result == {"action": "test", "report_path": "C:\\x.mht", "op_class": "write"}


async def test_cluster_test_rejects_bad_include() -> None:
    connector = WsfcConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await wsfc_cluster_test(connector, _TARGET, {"include": ["ok", ""]})
    run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# nodes group
# ---------------------------------------------------------------------------


async def test_node_list_builds_envelope() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"rows":[{"Name":"SQL01","State":"Up"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_node_list(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ClusterNode" in script
    assert "rows = $n; total = $n.Count" in script
    assert result["total"] == 1


async def test_node_state_escapes_name() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"name":"o\'db","state":"Up"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_node_state(connector, _TARGET, {"name": "o'db"})
    assert "Get-ClusterNode -Name 'o''db'" in _script_from_call(run_mock)
    assert result["state"] == "Up"


async def test_node_pause_drains_and_escapes_target() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"name":"SQL01","state":"Paused"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_node_pause(
            connector, _TARGET, {"name": "SQL01", "target_node": "SQL0'2"}
        )
    script = _script_from_call(run_mock)
    assert "Suspend-ClusterNode -Name 'SQL01' -Drain -TargetNode 'SQL0''2'" in script
    assert result == {
        "name": "SQL01",
        "action": "pause",
        "state": "Paused",
        "op_class": "write",
    }


async def test_node_pause_no_drain_omits_flag() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"name":"SQL01","state":"Paused"}')
    with patch.object(connector, "_run_command", run_mock):
        await wsfc_node_pause(connector, _TARGET, {"name": "SQL01", "drain": False})
    script = _script_from_call(run_mock)
    assert "Suspend-ClusterNode -Name 'SQL01'" in script
    assert "-Drain" not in script


async def test_node_resume_validates_failback() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"name":"SQL01","state":"Up"}')
    with patch.object(connector, "_run_command", run_mock):
        await wsfc_node_resume(connector, _TARGET, {"name": "SQL01", "failback": "Immediate"})
    assert "Resume-ClusterNode -Name 'SQL01' -Failback Immediate" in _script_from_call(run_mock)


async def test_node_resume_rejects_bad_failback() -> None:
    connector = WsfcConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await wsfc_node_resume(connector, _TARGET, {"name": "SQL01", "failback": "later"})
    run_mock.assert_not_awaited()


async def test_node_evict_builds_remove_force() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_node_evict(connector, _TARGET, {"name": "SQL0'2"})
    assert "Remove-ClusterNode -Name 'SQL0''2' -Force" in _script_from_call(run_mock)
    assert result == {"name": "SQL0'2", "action": "evict", "op_class": "write"}


# ---------------------------------------------------------------------------
# groups group
# ---------------------------------------------------------------------------


async def test_group_list_builds_envelope() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"rows":[{"Name":"SQL Server (MSSQLSERVER)","State":"Online"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_group_list(connector, _TARGET, {})
    assert "Get-ClusterGroup" in _script_from_call(run_mock)
    assert result["total"] == 1


async def test_group_state_escapes_and_reads_owner() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"name":"SQL Server (MSSQLSERVER)","state":"Online","owner_node":"SQL01"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_group_state(connector, _TARGET, {"name": "SQL Server (MSSQLSERVER)"})
    assert "Get-ClusterGroup -Name 'SQL Server (MSSQLSERVER)'" in _script_from_call(run_mock)
    assert result["state"] == "Online"
    assert result["owner_node"] == "SQL01"


async def test_group_move_builds_and_escapes_node() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"name":"role","state":"Online","owner_node":"SQL02"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_group_move(connector, _TARGET, {"name": "role", "node": "SQL0'2"})
    script = _script_from_call(run_mock)
    assert "Move-ClusterGroup -Name 'role' -Node 'SQL0''2'" in script
    assert result["action"] == "move"
    assert result["owner_node"] == "SQL02"


@pytest.mark.parametrize(
    ("handler", "cmdlet", "action"),
    [
        (wsfc_group_offline, "Stop-ClusterGroup", "offline"),
        (wsfc_group_online, "Start-ClusterGroup", "online"),
    ],
)
async def test_group_power_builds_cmdlet(handler: Any, cmdlet: str, action: str) -> None:
    connector = WsfcConnector()
    run_mock = _run(f'{{"ok":true,"name":"role","state":"{action}"}}')
    with patch.object(connector, "_run_command", run_mock):
        result = await handler(connector, _TARGET, {"name": "ro'le"})
    assert f"{cmdlet} -Name 'ro''le'" in _script_from_call(run_mock)
    assert result["action"] == action
    assert result["op_class"] == "write"


# ---------------------------------------------------------------------------
# resources group
# ---------------------------------------------------------------------------


async def test_resource_list_builds_projection() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"rows":[{"Name":"IP","State":"Online"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_resource_list(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ClusterResource" in script
    assert "ResourceType" in script
    assert result["total"] == 1


async def test_dependency_report_builds_per_resource_query() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"rows":[{"resource":"Name","dependency_expression":"([IP])"}],"total":1}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_resource_dependency_report(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ClusterResourceDependency -Resource $_.Name" in script
    assert result["total"] == 1


# ---------------------------------------------------------------------------
# witness group — incl. the secret-hygiene invariant
# ---------------------------------------------------------------------------


async def test_witness_get_resolves_resource_state() -> None:
    connector = WsfcConnector()
    run_mock = _run(
        '{"cluster":"c1sql1","quorum_type":"NodeAndFileShareMajority",'
        '"witness_resource":"File Share Witness","witness_state":"Online","online":true}'
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_witness_get(connector, _TARGET, {})
    script = _script_from_call(run_mock)
    assert "Get-ClusterQuorum" in script
    assert "Get-ClusterResource -Name $wname" in script
    assert result["online"] is True


async def test_witness_set_disk_escapes_resource() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"quorum_type":"NodeAndDiskMajority","quorum_resource":"Disk"}')
    with patch.object(connector, "_run_command", run_mock):
        result = await wsfc_witness_set(
            connector, _TARGET, {"witness_type": "disk", "resource": "Cluster Disk 'W'"}
        )
    script = _script_from_call(run_mock)
    assert "Set-ClusterQuorum -DiskWitness 'Cluster Disk ''W'''" in script
    assert result == {
        "action": "set",
        "witness_type": "disk",
        "quorum_type": "NodeAndDiskMajority",
        "quorum_resource": "Disk",
        "op_class": "write",
    }


async def test_witness_set_file_share_escapes_path() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"quorum_type":"NodeAndFileShareMajority","quorum_resource":"FSW"}')
    with patch.object(connector, "_run_command", run_mock):
        await wsfc_witness_set(
            connector, _TARGET, {"witness_type": "file_share", "path": "\\\\fs\\w'x"}
        )
    assert "Set-ClusterQuorum -FileShareWitness '\\\\fs\\w''x'" in _script_from_call(run_mock)


async def test_witness_set_node_majority_uses_no_witness() -> None:
    connector = WsfcConnector()
    run_mock = _run('{"ok":true,"quorum_type":"NodeMajority","quorum_resource":null}')
    with patch.object(connector, "_run_command", run_mock):
        await wsfc_witness_set(connector, _TARGET, {"witness_type": "node_majority"})
    assert "Set-ClusterQuorum -NoWitness" in _script_from_call(run_mock)


@pytest.mark.parametrize(
    "params",
    [
        {"witness_type": "cloud"},  # cloud witness excluded (secret can't ride the transport)
        {"witness_type": "disk"},  # missing resource
        {"witness_type": "file_share"},  # missing path
    ],
)
async def test_witness_set_rejects_bad_params(params: dict[str, Any]) -> None:
    connector = WsfcConnector()
    run_mock = _run("{}")
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await wsfc_witness_set(connector, _TARGET, params)
    run_mock.assert_not_awaited()


def test_no_write_op_exposes_a_secret_value_field() -> None:
    """The secret-leak guard at the schema level: no op accepts a plaintext
    secret-value parameter (password / secret / cloud-witness access key), so a
    secret can never reach the ``-EncodedCommand`` argv. (The cloud witness,
    whose ``-AccessKey`` is a secret, is deliberately not offered.)"""
    secret_fields = {"password", "secret", "access_key", "accesskey", "accountkey"}
    for op in WSFC_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


# ---------------------------------------------------------------------------
# about (fingerprint wrapper) + probe
# ---------------------------------------------------------------------------


async def test_about_maps_fingerprint_into_identity_dict() -> None:
    connector = WsfcConnector()
    payload = (
        '{"Hostname":"SQL01","OsVersion":"10.0.20348","BuildNumber":"20348",'
        '"PowerShellVersion":"5.1.20348.1","FailoverClustersModule":true,'
        '"ClusterName":"c1sql1","ClusterFunctionalLevel":11}'
    )
    run_mock = _run(payload)
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.about(_TARGET, {})
    assert "Get-Module -ListAvailable -Name FailoverClusters" in _script_from_call(run_mock)
    assert result["vendor"] == "microsoft"
    assert result["product"] == "windows-failover-cluster"
    assert result["version"] == "10.0.20348"
    assert result["build"] == "20348"
    assert result["hostname"] == "SQL01"
    assert result["cluster_name"] == "c1sql1"
    assert result["cluster_functional_level"] == 11
    assert result["failover_clusters_module"] is True
    assert result["powershell_version"] == "5.1.20348.1"


async def test_probe_failover_module_absent() -> None:
    connector = WsfcConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":false,"cluster":null}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "failover_module_absent"


async def test_probe_not_cluster_member() -> None:
    connector = WsfcConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":true,"cluster":null}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "not_cluster_member"


async def test_probe_ok_when_member() -> None:
    connector = WsfcConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", _run('{"module":true,"cluster":"c1sql1"}')),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None


@pytest.mark.parametrize(
    "boom",
    [
        OSError("connection reset by peer"),
        asyncssh.ConnectionLost("channel closed mid-command"),
        TimeoutError("probe script timed out"),
    ],
)
async def test_probe_command_failed_when_run_command_raises_after_connect(boom: Exception) -> None:
    connector = WsfcConnector()
    with (
        patch.object(connector, "_connect", AsyncMock(return_value=MagicMock())),
        patch.object(connector, "_run_command", AsyncMock(side_effect=boom)),
    ):
        result = await connector.probe(_TARGET)
    assert result.ok is False
    assert result.reason == "command_failed"


async def test_probe_tcp_unreachable_and_auth_failed() -> None:
    connector = WsfcConnector()
    with patch.object(connector, "_connect", AsyncMock(side_effect=OSError("no route"))):
        assert (await connector.probe(_TARGET)).reason == "tcp_unreachable"
    with patch.object(
        connector, "_connect", AsyncMock(side_effect=asyncssh.PermissionDenied("no"))
    ):
        assert (await connector.probe(_TARGET)).reason == "ssh_auth_failed"


async def test_fingerprint_unreachable_is_not_an_exception() -> None:
    connector = WsfcConnector()
    with patch.object(connector, "_run_command", AsyncMock(side_effect=OSError("down"))):
        result = await connector.fingerprint(_TARGET)
    assert result.reachable is False
    assert result.vendor == "microsoft"
    assert result.product == "windows-failover-cluster"
    assert "error" in result.extras

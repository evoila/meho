# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``holodeck.instance.remove`` and the ``leftover_vms`` field of
``holodeck.instance.status``.

The HoloRouter is not called. ``pwsh_run`` and ``_run_command`` are mocked with
hand-written answers (made-up names and ids), the same way as
``test_connectors_holodeck_deploy.py``.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors._shared.pwsh import PwshRunError
from meho_backplane.connectors.holodeck import HOLODECK_OPS, HolodeckConnector
from meho_backplane.connectors.holodeck.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.holodeck.ops_remove import (
    REMOVE_OPS,
    _instance_remove_preview,
    file_name_pattern,
    instance_vms,
    read_remove_record,
)
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings

_PWSH = "meho_backplane.connectors.holodeck.ops_remove.pwsh_run"
_STATUS_PWSH = "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run"
# The made-up password starts with the instance prefix on purpose: a VM search
# that reads every text would take it for a VM name.
_SECRET = "lab01-not-a-real-password-123"
_RECORD_CMD = "cat /holodeck-runtime/logs/lab01-remove.json"


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
    name: str = "holorouter-remove-test"
    host: str = "holorouter.test.invalid"
    port: int | None = 22
    secret_ref: str = "meho/testing/holodeck/holorouter-remove-test"
    version: str | None = "9.0.2.0"
    fingerprint: dict[str, Any] = field(default_factory=dict)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _instance(instance_id: str = "lab01") -> dict[str, Any]:
    """A made-up ``Get-HoloDeckInstance`` answer, with a password in it."""
    return {
        "InstanceID": instance_id,
        "Status": "Completed",
        "Target": {"hostname": "vcenter.example.invalid", "password": _SECRET},
        "NestedHosts": [
            {"Name": f"{instance_id}-esx-01a", "Host": "esx-a.example.invalid"},
            {"Name": f"{instance_id}-esx-02a", "Host": "esx-a.example.invalid"},
        ],
        "Installer": {"VMName": f"{instance_id}-vcfinstaller-a", "Datastore": "ds-01"},
    }


def _check(**overrides: Any) -> dict[str, Any]:
    """A made-up answer of the read-only check script."""
    payload: dict[str, Any] = {
        "has_remove": True,
        "has_instance_param": True,
        "has_reset_param": True,
        "has_get": True,
        "instance": _instance(),
        "lookup_error": None,
        "config_ids": ["cfg0001"],
        "files": [
            "/holodeck-runtime/config/cfg0001.json",
            "/holodeck-runtime/state/lab01-state.json",
        ],
    }
    payload.update(overrides)
    return payload


def _decoded_script(launch: str) -> str:
    """Return the PowerShell script inside the tmux launch command."""
    argv = shlex.split(launch)
    assert argv[:4] == ["tmux", "new-session", "-d", "-s"]
    remote = shlex.split(argv[5])
    encoded = remote[remote.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def _record_stdout(record: dict[str, Any], *, tmux_alive: bool | None) -> str:
    """What the record read command prints: the file, then the tmux session list."""
    text = json.dumps(record)
    if tmux_alive is None:
        return text
    sessions = "holodeck-deploy-other\nholodeck-remove-lab01\n" if tmux_alive else ""
    return f"{text}\n\n__tmux_sessions__\n{sessions}"


def _interrupted_record(**overrides: Any) -> dict[str, Any]:
    """A progress file of a remove whose script was killed by the router reboot.

    The cmdlet had finished (``deleted`` true). Pass ``deleted=None`` for a
    script that died before or inside the cmdlet.
    """
    record: dict[str, Any] = {
        "instance_id": "lab01",
        "state": "running",
        "started_at": "2026-01-01T10:00:00.1234567Z",
        "finished_at": None,
        "reset_router": True,
        "resumed": False,
        "error": None,
        "deleted": True,
        "leftover_vms": None,
        "leftover_error": None,
        "moved_to": None,
        "moved_files": [],
        "move_errors": [],
    }
    record.update(overrides)
    return record


def _commands(*, record: dict[str, Any] | None, tmux_alive: bool | None, exit_status: int) -> Any:
    """Answer the record read and the tmux launch of ``_run_command``."""

    async def run(target: Any, cmd: str, operator: Any = None) -> Any:
        if cmd.startswith("cat "):
            if record is None:
                return _proc(exit_status=1)
            return _proc(stdout=_record_stdout(record, tmux_alive=tmux_alive))
        return _proc(stderr="duplicate session", exit_status=exit_status)

    return run


def _launches(mock_cmd: Any) -> list[str]:
    return [c.args[1] for c in mock_cmd.call_args_list if c.args[1].startswith("tmux ")]


async def _remove(
    params: dict[str, Any],
    check: Any,
    *,
    exit_status: int = 0,
    record: dict[str, Any] | None = None,
    tmux_alive: bool | None = False,
) -> tuple[Any, Any]:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        if isinstance(check, Exception):
            mock_pwsh.side_effect = check
        else:
            mock_pwsh.return_value = check
        mock_cmd.side_effect = _commands(
            record=record, tmux_alive=tmux_alive, exit_status=exit_status
        )
        result = await connector.instance_remove(_StubTarget(), params)
    return result, mock_cmd


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_remove_op_is_registered_destructive_with_approval() -> None:
    (op,) = REMOVE_OPS
    assert op in HOLODECK_OPS
    assert op.op_id == "holodeck.instance.remove"
    assert op.safety_level == "destructive"
    assert op.requires_approval is True
    assert op.group_key in _WHEN_TO_USE_BY_GROUP
    assert "holodeck.instance.remove" in _WHEN_TO_USE_BY_GROUP[op.group_key]
    assert hasattr(HolodeckConnector, op.handler_attr)
    assert "holodeck.instance.remove" in _PREVIEW_BUILDERS


def test_remove_op_metadata_is_complete() -> None:
    (op,) = REMOVE_OPS
    instr = op.llm_instructions or {}
    for key in ("when_to_call", "output_shape", "next_step", "preconditions"):
        assert instr.get(key), key
    assert "Holodeck has no REST API" in instr["when_to_call"]
    for name, spec in op.parameter_schema["properties"].items():
        assert spec.get("description"), name
    assert op.response_schema and op.response_schema["properties"]
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []


@pytest.mark.parametrize("bad_id", ["lab-01", "toolongid9", "", "a b", "x';rm"])
def test_remove_schema_rejects_bad_instance_id(bad_id: str) -> None:
    (op,) = REMOVE_OPS
    assert validate_params(op.parameter_schema, {"instance_id": bad_id})


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_refuses_bad_instance_id_before_any_command() -> None:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        result = await connector.instance_remove(_StubTarget(), {"instance_id": "lab01\n"})
    assert result["status"] == "refused"
    mock_pwsh.assert_not_called()
    mock_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_remove_success_starts_detached_without_reset() -> None:
    result, mock_cmd = await _remove({"instance_id": "lab01"}, _check())
    assert result["status"] == "started"
    assert result["config_id"] == "cfg0001"
    assert result["reset_router"] is False
    assert result["vms"] == ["lab01-esx-01a", "lab01-esx-02a", "lab01-vcfinstaller-a"]
    assert result["start_marker"]["tmux_session"] == "holodeck-remove-lab01"
    assert result["start_marker"]["record_path"] == "/holodeck-runtime/logs/lab01-remove.json"
    launch = mock_cmd.call_args.args[1]
    script = _decoded_script(launch)
    assert "Import-HoloDeckConfig -ConfigID 'cfg0001'" in script
    assert "Remove-HoloDeckInstance -InstanceID $id -ErrorAction Stop" in script
    assert "-ResetHoloRouter" not in script
    assert _SECRET not in json.dumps(result)
    assert _SECRET not in script


@pytest.mark.asyncio
async def test_remove_with_router_reset_adds_the_switch() -> None:
    result, mock_cmd = await _remove({"instance_id": "lab01", "reset_router": True}, _check())
    assert result["status"] == "started"
    assert result["reset_router"] is True
    script = _decoded_script(mock_cmd.call_args.args[1])
    assert "Remove-HoloDeckInstance -InstanceID $id -ResetHoloRouter -ErrorAction Stop" in script


@pytest.mark.asyncio
async def test_remove_moves_state_files_into_a_dated_folder() -> None:
    result, mock_cmd = await _remove({"instance_id": "lab01"}, _check())
    assert result["files_to_move"] == [
        "/holodeck-runtime/config/cfg0001.json",
        "/holodeck-runtime/state/lab01-state.json",
    ]
    script = _decoded_script(mock_cmd.call_args.args[1])
    assert "Move-Item -LiteralPath $p" in script
    assert "'/holodeck-runtime/removed/lab01-'" in script
    assert "'/holodeck-runtime/state/lab01-state.json'" in script
    # Files move only when the delete did not fail.
    assert "if (-not $r.error" in script


@pytest.mark.asyncio
async def test_remove_can_keep_state_files() -> None:
    result, mock_cmd = await _remove({"instance_id": "lab01", "move_state_files": False}, _check())
    assert result["status"] == "started"
    assert result["files_to_move"] == []
    assert "Move-Item" not in _decoded_script(mock_cmd.call_args.args[1])


@pytest.mark.asyncio
async def test_remove_unknown_instance_is_unchanged() -> None:
    result, mock_cmd = await _remove({"instance_id": "gone01"}, _check(instance=None))
    assert result["status"] == "unchanged"
    # Only the progress file is read; nothing starts.
    assert [c.args[1] for c in mock_cmd.call_args_list] == [
        "cat /holodeck-runtime/logs/gone01-remove.json && { echo; echo __tmux_sessions__; "
        "tmux list-sessions -F '#S' 2>/dev/null; true; }"
    ]


@pytest.mark.asyncio
async def test_remove_refuses_a_listed_instance_without_vm_names() -> None:
    bare = {"InstanceID": "lab01", "Status": "Completed", "Target": {"password": _SECRET}}
    result, mock_cmd = await _remove({"instance_id": "lab01"}, _check(instance=bare, state=[]))
    assert result["status"] == "refused"
    assert "state file" in result["reason"]
    assert "Nothing was deleted" in result["reason"]
    assert _SECRET not in json.dumps(result)
    # Only the progress file is read; nothing starts.
    assert _launches(mock_cmd) == []


@pytest.mark.asyncio
async def test_remove_takes_vms_from_the_state_file() -> None:
    bare = {"InstanceID": "lab01", "Status": "Completed"}
    state = {
        "Target": {"hostname": "vcenter.example.invalid", "password": _SECRET},
        "Hosts": [{"Name": "lab01-esx-01a", "Host": "esx-a.example.invalid"}],
        "Installer": {"VMName": "lab01-vcfinstaller-a"},
    }
    result, mock_cmd = await _remove({"instance_id": "lab01"}, _check(instance=bare, state=state))
    assert result["status"] == "started"
    assert result["vms"] == ["lab01-esx-01a", "lab01-vcfinstaller-a"]
    assert _SECRET not in json.dumps(result)
    assert len(_launches(mock_cmd)) == 1


@pytest.mark.asyncio
async def test_check_script_reads_the_state_files() -> None:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock, return_value=_check()) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        mock_cmd.side_effect = _commands(record=None, tmux_alive=None, exit_status=0)
        await connector.instance_remove(_StubTarget(), {"instance_id": "lab01"})
    script = mock_pwsh.call_args.args[2]
    assert "state = @() }" in script
    assert (
        "if ($d -eq '/holodeck-runtime/state' -and $f.Extension -eq '.json') { try { "
        "$out.state += ,(Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json) }"
    ) in script


@pytest.mark.asyncio
async def test_remove_resumes_an_interrupted_run() -> None:
    result, mock_cmd = await _remove(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=None),
        record=_interrupted_record(),
        tmux_alive=False,
    )
    assert result["status"] == "started"
    assert result["resumed"] is True
    assert result["reset_router"] is False
    assert result["vms"] == []
    assert result["files_to_move"] == [
        "/holodeck-runtime/config/cfg0001.json",
        "/holodeck-runtime/state/lab01-state.json",
    ]
    (launch,) = _launches(mock_cmd)
    script = _decoded_script(launch)
    assert "Remove-HoloDeckInstance" not in script
    assert "Import-HoloDeckConfig -ConfigID 'cfg0001'" in script
    assert "Get-VM -Name ($id + '-*') -ErrorAction Stop" in script
    assert "Move-Item -LiteralPath $p" in script
    assert "started_at = '2026-01-01T10:00:00.1234567Z';" in script
    assert "reset_router = $true; resumed = $true;" in script
    assert "deleted = $true;" in script


def _listed(start_time: str | None) -> dict[str, Any]:
    """A listed instance, deployed at *start_time* (the record started at 10:00)."""
    instance = _instance()
    if start_time is not None:
        instance["StartTime"] = start_time
    return instance


@pytest.mark.asyncio
async def test_remove_resumes_when_the_old_instance_is_still_listed() -> None:
    # The cmdlet finished, but the state file still lists the instance until it moves.
    result, mock_cmd = await _remove(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=_listed("2026-01-01T09:00:00Z")),
        record=_interrupted_record(),
        tmux_alive=False,
    )
    assert result["status"] == "started"
    assert result["resumed"] is True
    assert result["reset_router"] is False
    assert result["vms"] == []
    (launch,) = _launches(mock_cmd)
    script = _decoded_script(launch)
    assert "Remove-HoloDeckInstance" not in script
    assert "-ResetHoloRouter" not in script
    assert "Move-Item -LiteralPath $p" in script
    assert "deleted = $true;" in script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "start_time"),
    [
        # The script died before or inside the cmdlet: the delete is not confirmed.
        (_interrupted_record(deleted=None), "2026-01-01T09:00:00Z"),
        # The record belongs to an older deploy with the same id.
        (_interrupted_record(), "2026-01-02T08:00:00Z"),
        # MEHO cannot compare the two times.
        (_interrupted_record(), None),
    ],
    ids=["delete-not-confirmed", "older-record", "no-deploy-time"],
)
async def test_remove_deletes_a_listed_instance_again_when_the_run_cannot_be_resumed(
    record: dict[str, Any], start_time: str | None
) -> None:
    result, mock_cmd = await _remove(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=_listed(start_time)),
        record=record,
        tmux_alive=False,
    )
    assert result["status"] == "started"
    assert result["resumed"] is False
    assert result["reset_router"] is True
    assert result["vms"] == ["lab01-esx-01a", "lab01-esx-02a", "lab01-vcfinstaller-a"]
    (launch,) = _launches(mock_cmd)
    script = _decoded_script(launch)
    assert "Remove-HoloDeckInstance -InstanceID $id -ResetHoloRouter -ErrorAction Stop" in script
    assert "resumed = $false;" in script
    assert "deleted = $null;" in script


@pytest.mark.asyncio
async def test_remove_finishes_an_unconfirmed_run_when_the_instance_is_gone() -> None:
    # Nothing is left to delete; the leftover VM list shows what is left, and
    # the record keeps that the delete was never confirmed.
    result, mock_cmd = await _remove(
        {"instance_id": "lab01"},
        _check(instance=None),
        record=_interrupted_record(deleted=None),
        tmux_alive=False,
    )
    assert result["status"] == "started"
    assert result["resumed"] is True
    (launch,) = _launches(mock_cmd)
    script = _decoded_script(launch)
    assert "Remove-HoloDeckInstance" not in script
    assert "deleted = $null;" in script
    assert "Get-VM -Name ($id + '-*') -ErrorAction Stop" in script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "tmux_alive"),
    [
        (_interrupted_record(), True),  # the remove still runs
        (_interrupted_record(), None),  # MEHO could not list the tmux sessions
        (_interrupted_record(state="done"), False),  # finished: nothing to resume
        (_interrupted_record(error="VM is locked"), False),  # the cmdlet failed
    ],
)
async def test_remove_unknown_instance_with_a_record_that_is_not_interrupted_is_unchanged(
    record: dict[str, Any], tmux_alive: bool | None
) -> None:
    result, mock_cmd = await _remove(
        {"instance_id": "lab01"}, _check(instance=None), record=record, tmux_alive=tmux_alive
    )
    assert result["status"] == "unchanged"
    assert _launches(mock_cmd) == []


@pytest.mark.asyncio
async def test_remove_script_saves_the_record_right_after_the_cmdlet() -> None:
    _, mock_cmd = await _remove({"instance_id": "lab01", "reset_router": True}, _check())
    script = _decoded_script(mock_cmd.call_args.args[1])
    # The cmdlet's outcome is on disk before the router reboot can kill the script:
    # deleted is null until the cmdlet returns, then true, or false with the error.
    assert "error = $null; deleted = $null; leftover_vms = $null;" in script
    assert (
        "Remove-HoloDeckInstance -InstanceID $id -ResetHoloRouter -ErrorAction Stop; "
        "$r.deleted = $true } "
        "catch { $r.error = $_.Exception.Message; $r.deleted = $false }; Save-Record; "
    ) in script
    assert "resumed = $false;" in script


@pytest.mark.asyncio
async def test_remove_script_records_failed_moves() -> None:
    _, mock_cmd = await _remove({"instance_id": "lab01"}, _check())
    script = _decoded_script(mock_cmd.call_args.args[1])
    assert "move_errors = @() }" in script
    assert (
        "try { $sub = Join-Path $dest (Split-Path -Parent $p).Substring(18); "
        "$null = [System.IO.Directory]::CreateDirectory($sub); "
        "Move-Item -LiteralPath $p -Destination $sub -Force -ErrorAction Stop; "
        "$r.moved_files += $p } "
        "catch { $r.move_errors += ($p + ': ' + $_.Exception.Message) }"
    ) in script
    assert "if ($r.moved_files.Count -gt 0) { $r.moved_to = $dest }" in script


@pytest.mark.asyncio
async def test_remove_script_stops_on_a_failed_vm_read() -> None:
    _, mock_cmd = await _remove({"instance_id": "lab01"}, _check())
    script = _decoded_script(mock_cmd.call_args.args[1])
    assert "leftover_vms = $null;" in script
    assert (
        "$r.leftover_vms = @(Get-VM -Name ($id + '-*') -ErrorAction Stop "
        "| ForEach-Object { $_.Name }) } catch { $r.leftover_error = $_.Exception.Message }"
    ) in script
    assert "-ErrorAction SilentlyContinue" not in script


@pytest.mark.asyncio
async def test_read_remove_record_tells_if_the_tmux_session_is_alive() -> None:
    connector = HolodeckConnector()
    target = _StubTarget()
    cases: list[tuple[str, bool | None]] = [
        (_record_stdout(_interrupted_record(), tmux_alive=True), True),
        (_record_stdout(_interrupted_record(), tmux_alive=False), False),
        (_record_stdout(_interrupted_record(), tmux_alive=None), None),
    ]
    for stdout, expected in cases:
        with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = _proc(stdout=stdout)
            record = await read_remove_record(connector, target, "lab01", None)
        assert record is not None
        assert record["tmux_alive"] is expected
        assert record["state"] == "running"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(exit_status=1)
        assert await read_remove_record(connector, target, "lab01", None) is None


@pytest.mark.asyncio
async def test_remove_check_error_text_reaches_the_answer() -> None:
    error = PwshRunError("Remove-HoloDeckInstance: access denied", exit_status=1, stderr="")
    result, mock_cmd = await _remove({"instance_id": "lab01"}, error)
    assert result["status"] == "refused"
    assert "access denied" in result["reason"]
    mock_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_remove_lookup_error_is_refused_not_unchanged() -> None:
    result, mock_cmd = await _remove(
        {"instance_id": "lab01"}, _check(instance=None, lookup_error="config not loaded")
    )
    assert result["status"] == "refused"
    assert "config not loaded" in result["reason"]
    mock_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_remove_tmux_failure_is_refused_with_error_text() -> None:
    result, _ = await _remove({"instance_id": "lab01"}, _check(), exit_status=1)
    assert result["status"] == "refused"
    assert "duplicate session" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"has_remove": False, "has_instance_param": False}, "9.0.1"),
        ({"has_instance_param": False}, "9.0.1"),
        ({"has_get": False}, "9.0.2"),
    ],
)
async def test_remove_older_toolkit_gets_a_version_message(
    overrides: dict[str, Any], needle: str
) -> None:
    result, mock_cmd = await _remove({"instance_id": "lab01"}, _check(**overrides))
    assert result["status"] == "refused"
    assert needle in result["reason"]
    assert "9.0.2.0" in result["reason"]  # the version the target reports
    mock_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_remove_reset_without_the_switch_is_refused() -> None:
    result, mock_cmd = await _remove(
        {"instance_id": "lab01", "reset_router": True}, _check(has_reset_param=False)
    )
    assert result["status"] == "refused"
    assert "-ResetHoloRouter" in result["reason"]
    mock_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_remove_config_choice() -> None:
    result, _ = await _remove({"instance_id": "lab01"}, _check(config_ids=[]))
    assert result["status"] == "refused" and "config_id" in result["reason"]
    result, _ = await _remove({"instance_id": "lab01"}, _check(config_ids=["cfgA", "cfgB"]))
    assert result["status"] == "refused" and "cfgA, cfgB" in result["reason"]
    result, mock_cmd = await _remove(
        {"instance_id": "lab01", "config_id": "cfg9"}, _check(config_ids=["cfgA", "cfgB"])
    )
    assert result["status"] == "started" and result["config_id"] == "cfg9"
    assert "-ConfigID 'cfg9'" in _decoded_script(mock_cmd.call_args.args[1])


@pytest.mark.parametrize(
    ("instance_id", "name", "matches"),
    [
        ("lab0", "lab0-state.json", True),
        ("lab0", "lab0.json", True),
        ("lab0", "deploy_LAB0.log", True),
        ("lab0", "lab01-state.json", False),
        ("lab0", "lab01.json", False),
        ("lab0", "xlab0-state.json", False),
        ("lab01", "lab01-state.json", True),
        ("lab01", "lab0-state.json", False),
    ],
)
def test_file_name_match_takes_the_id_only_as_a_whole_word(
    instance_id: str, name: str, matches: bool
) -> None:
    # PowerShell -match ignores case, so the Python check does too.
    pattern = file_name_pattern(instance_id)
    assert bool(re.search(pattern, name, re.IGNORECASE)) is matches


@pytest.mark.asyncio
async def test_check_script_uses_the_whole_word_file_match() -> None:
    connector = HolodeckConnector()
    for instance_id in ("lab0", "lab01"):
        with patch(_PWSH, new_callable=AsyncMock) as mock_pwsh:
            mock_pwsh.return_value = _check(instance=None)
            await connector.instance_remove(_StubTarget(), {"instance_id": instance_id})
        script = mock_pwsh.call_args.args[2]
        assert f"$word = '{file_name_pattern(instance_id)}';" in script
        assert "$f.Name -match $word" in script
        assert "-like" not in script


def test_instance_vms_keeps_only_names_and_locations() -> None:
    vms = instance_vms(_instance(), "lab01")
    assert vms == [
        {"name": "lab01-esx-01a", "location": "esx-a.example.invalid"},
        {"name": "lab01-esx-02a", "location": "esx-a.example.invalid"},
        {"name": "lab01-vcfinstaller-a", "location": "ds-01"},
    ]
    assert _SECRET not in json.dumps(vms)
    assert instance_vms(None, "lab01") == []
    assert instance_vms([{"Name": "other-esx-01a"}], "lab01") == []


def test_instance_vms_skips_secret_fields_and_bare_texts() -> None:
    record = {
        "Password": "lab01-password-looks-like-a-vm",
        "Target": {"Token": "lab01-token", "ApiSecret": "lab01-secret", "Pwd": "lab01-pwd"},
        "Credentials": {"Name": "lab01-under-a-secret-key"},
        "Notes": ["lab01-a-bare-text", {"Comment": "lab01-not-a-name-field"}],
        "Hosts": [{"Name": "lab01-esx-01a", "Host": {"Password": "lab01-x"}}],
    }
    assert instance_vms(record, "lab01") == [{"name": "lab01-esx-01a", "location": None}]


# ---------------------------------------------------------------------------
# Status: leftover_vms
# ---------------------------------------------------------------------------


async def _status(
    record: dict[str, Any] | None, pwsh: Any, *, tmux_alive: bool | None = None
) -> dict[str, Any]:
    connector = HolodeckConnector()
    stdout = _record_stdout(record, tmux_alive=tmux_alive) if record is not None else ""
    with (
        patch(_STATUS_PWSH, new_callable=AsyncMock) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        if isinstance(pwsh, Exception):
            mock_pwsh.side_effect = pwsh
        else:
            mock_pwsh.return_value = pwsh
        mock_cmd.return_value = _proc(stdout=stdout, exit_status=0 if record else 1)
        result = await connector.instance_status(_StubTarget(), {"instance_id": "lab01"})
    mock_cmd.assert_called_once()
    assert _RECORD_CMD in mock_cmd.call_args.args[1]
    assert "tmux list-sessions -F '#S'" in mock_cmd.call_args.args[1]
    return result


@pytest.mark.asyncio
async def test_status_interrupted_remove_is_removed_with_a_resume_note() -> None:
    result = await _status(_interrupted_record(), None, tmux_alive=False)
    assert result["status"] == "removed"
    assert result["leftover_vms"] is None
    assert result["removal"]["state"] == "interrupted"
    assert result["removal"]["deleted"] is True
    assert result["removal"]["reset_router"] is True
    assert "error" not in result
    (note,) = result["notes"]
    assert "tmux session is gone" in note
    assert "call holodeck.instance.remove again" in note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        # Killed before the cmdlet: the instance is still listed.
        (
            {"InstanceID": "lab01", "Status": "Completed", "StartTime": "2026-01-01T09:00:00Z"},
            "completed",
        ),
        # Killed inside the cmdlet: the instance may be gone, or half gone.
        (None, "unknown"),
    ],
    ids=["before-the-cmdlet", "inside-the-cmdlet"],
)
async def test_status_interrupted_before_the_delete_finished_is_not_removed(
    listed: dict[str, Any] | None, expected: str
) -> None:
    result = await _status(_interrupted_record(deleted=None), listed, tmux_alive=False)
    assert result["status"] == expected
    assert result["removal"]["state"] == "interrupted"
    assert result["removal"]["deleted"] is None
    assert result["leftover_vms"] is None
    (note,) = result["notes"]
    assert "cannot tell whether the VMs were deleted" in note
    assert "call holodeck.instance.remove again" in note


@pytest.mark.asyncio
async def test_status_resumed_run_without_a_confirmed_delete_is_not_removed() -> None:
    record = _interrupted_record(
        state="done",
        resumed=True,
        deleted=None,
        finished_at="2026-01-01T10:40:00Z",
        leftover_vms=["lab01-esx-01a"],
        moved_to="/holodeck-runtime/removed/lab01-20260101-104000",
        moved_files=["/holodeck-runtime/state/lab01-state.json"],
    )
    result = await _status(record, None, tmux_alive=False)
    assert result["status"] == "unknown"
    assert result["removal"]["deleted"] is None
    assert result["leftover_vms"] == ["lab01-esx-01a"]
    assert any("cannot confirm that the VMs were deleted" in note for note in result["notes"])
    assert any("vmware.composite.vm.destroy" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_running_remove_with_a_live_session_is_running() -> None:
    result = await _status(_interrupted_record(), {"Status": "Completed"}, tmux_alive=True)
    assert result["status"] == "completed"
    assert result["removal"]["state"] == "running"
    assert result["leftover_vms"] is None
    assert any(note.startswith("a remove is running") for note in result["notes"])


@pytest.mark.asyncio
async def test_status_interrupted_remove_with_a_cmdlet_error_is_not_removed() -> None:
    record = _interrupted_record(error="VM is locked")
    result = await _status(record, None, tmux_alive=False)
    assert result["status"] == "unknown"
    assert result["removal"]["state"] == "interrupted"
    assert any("the last remove failed: VM is locked" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_resumed_remove_is_removed() -> None:
    record = _interrupted_record(
        state="done",
        resumed=True,
        finished_at="2026-01-01T10:40:00Z",
        leftover_vms=[],
        moved_to="/holodeck-runtime/removed/lab01-20260101-104000",
        moved_files=["/holodeck-runtime/state/lab01-state.json"],
    )
    result = await _status(record, None, tmux_alive=False)
    assert result["status"] == "removed"
    assert result["removal"]["resumed"] is True
    assert result["leftover_vms"] == []
    assert result["notes"] == []


@pytest.mark.asyncio
async def test_status_tells_an_empty_vm_search_from_a_failed_read() -> None:
    empty = _interrupted_record(state="done", leftover_vms=[])
    result = await _status(empty, None)
    assert result["status"] == "removed"
    assert result["leftover_vms"] == []
    assert result["notes"] == []

    failed = _interrupted_record(state="done", leftover_vms=None, leftover_error="not connected")
    result = await _status(failed, None)
    assert result["status"] == "removed"
    assert result["leftover_vms"] is None
    assert result["removal"]["leftover_error"] == "not connected"
    (note,) = result["notes"]
    assert "could not list the VMs that are left: not connected" in note


@pytest.mark.asyncio
async def test_status_reports_files_that_did_not_move() -> None:
    record = _interrupted_record(
        state="done",
        leftover_vms=[],
        moved_to="/holodeck-runtime/removed/lab01-20260101-102000",
        moved_files=["/holodeck-runtime/config/cfg0001.json"],
        move_errors=["/holodeck-runtime/state/lab01-state.json: Access denied"],
    )
    result = await _status(record, None)
    assert result["status"] == "removed"
    assert result["removal"]["moved_files"] == ["/holodeck-runtime/config/cfg0001.json"]
    assert result["removal"]["move_errors"] == [
        "/holodeck-runtime/state/lab01-state.json: Access denied"
    ]
    (note,) = result["notes"]
    assert note.startswith("1 file(s) did not move: /holodeck-runtime/state/lab01-state.json")


@pytest.mark.asyncio
async def test_status_reports_removed_and_leftover_vms() -> None:
    record = {
        "instance_id": "lab01",
        "state": "done",
        "started_at": "2026-01-01T10:00:00Z",
        "finished_at": "2026-01-01T10:20:00Z",
        "reset_router": False,
        "error": None,
        "deleted": True,
        "leftover_vms": ["lab01-vcfinstaller-a"],
        "leftover_error": None,
        "moved_to": "/holodeck-runtime/removed/lab01-20260101-102000",
        "moved_files": ["/holodeck-runtime/state/lab01-state.json"],
    }
    result = await _status(record, None)
    assert result["status"] == "removed"
    assert result["leftover_vms"] == ["lab01-vcfinstaller-a"]
    assert result["removal"]["moved_files"] == ["/holodeck-runtime/state/lab01-state.json"]
    assert "error" not in result
    assert any("vmware.composite.vm.destroy" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_failed_read_keeps_status_unknown_and_the_error() -> None:
    failed = PwshRunError("stdout was not valid JSON", exit_status=0, stderr="")
    result = await _status(_OLD_REMOVE, failed)
    assert result["status"] == "unknown"
    assert "not valid JSON" in result["error"]
    assert result["removal"]["state"] == "done"
    assert any("could not read Get-HoloDeckInstance" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_script_tells_not_listed_from_a_failed_read() -> None:
    connector = HolodeckConnector()
    with (
        patch(_STATUS_PWSH, new_callable=AsyncMock, return_value=None) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        mock_cmd.return_value = _proc(exit_status=1)
        result = await connector.instance_status(_StubTarget(), {"instance_id": "lab01"})
    script = mock_pwsh.call_args.args[2]
    assert "try { $r = @(Get-HoloDeckInstance -InstanceID 'lab01' -ErrorAction Stop) }" in script
    assert "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }" in script
    assert "{ 'null' }" in script
    assert result["status"] == "unknown"
    assert "error" not in result


@pytest.mark.asyncio
async def test_status_shows_the_remove_error_text() -> None:
    record = {"state": "failed", "error": "VM is locked", "leftover_vms": None}
    result = await _status(record, {"Status": "Completed"})
    assert result["status"] == "completed"
    assert result["leftover_vms"] is None
    assert result["removal"]["error"] == "VM is locked"
    assert any("VM is locked" in note for note in result["notes"])


_OLD_REMOVE = {
    "instance_id": "lab01",
    "state": "done",
    "started_at": "2026-01-01T10:00:00.1234567Z",
    "finished_at": "2026-01-01T10:20:00.1234567Z",
    "error": None,
    "deleted": True,
    "leftover_vms": ["lab01-vcfinstaller-a"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deploy_status", "expected"), [("InProgress", "running"), ("Completed", "completed")]
)
async def test_status_old_remove_record_does_not_hide_a_newer_deploy(
    deploy_status: str, expected: str
) -> None:
    deploy = {"InstanceID": "lab01", "Status": deploy_status, "StartTime": "2026-01-02T08:00:00Z"}
    result = await _status(_OLD_REMOVE, deploy)
    assert result["status"] == expected
    assert result["leftover_vms"] is None
    assert result["removal"]["state"] == "done"
    assert any("older remove record" in note for note in result["notes"])
    assert not any("vmware.composite.vm.destroy" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_old_remove_record_keeps_a_failed_deploy() -> None:
    deploy = {
        "InstanceID": "lab01",
        "Status": "Failed",
        "StartTime": "2026-01-02T08:00:00Z",
        "Message": "host not reachable",
    }
    result = await _status(_OLD_REMOVE, deploy)
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_status_remove_newer_than_the_listed_deploy_is_removed() -> None:
    deploy = {"InstanceID": "lab01", "Status": "Completed", "StartTime": "2025-12-31T08:00:00Z"}
    result = await _status(_OLD_REMOVE, deploy)
    assert result["status"] == "removed"
    assert result["leftover_vms"] == ["lab01-vcfinstaller-a"]


@pytest.mark.asyncio
async def test_status_listed_deploy_without_a_time_is_not_marked_removed() -> None:
    result = await _status(_OLD_REMOVE, {"InstanceID": "lab01", "Status": "InProgress"})
    assert result["status"] == "running"
    assert any("cannot tell which is newer" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_status_without_remove_record_is_unchanged() -> None:
    result = await _status(None, {"Status": "InProgress"})
    assert result["status"] == "running"
    assert "leftover_vms" not in result
    assert "removal" not in result


# ---------------------------------------------------------------------------
# Approval preview (blast radius)
# ---------------------------------------------------------------------------


def _ctx(connector: HolodeckConnector, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(),
        connector_instance=connector,
        operator=MagicMock(),
        target=_StubTarget(),
        params=params,
    )


async def _preview(
    params: dict[str, Any],
    check: dict[str, Any],
    *,
    record: dict[str, Any] | None = None,
    tmux_alive: bool | None = False,
) -> dict[str, Any] | None:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock, return_value=check),
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        mock_cmd.side_effect = _commands(record=record, tmux_alive=tmux_alive, exit_status=0)
        return await _instance_remove_preview(_ctx(connector, params))


@pytest.mark.asyncio
async def test_preview_lists_vms_router_reset_and_files() -> None:
    effect = await _preview({"instance_id": "lab01", "reset_router": True}, _check())
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    radius = effect["blast_radius"]
    assert radius["object"] == {
        "kind": "holodeck_instance",
        "instance_id": "lab01",
        "found": True,
        "resume": False,
    }
    assert radius["irreversibility"] == "permanent"
    kinds = [child["kind"] for child in radius["children"]]
    assert kinds.count("vm") == 3
    assert "router_reset" in kinds
    assert kinds.count("file_move") == 2
    assert _SECRET not in json.dumps(effect)


@pytest.mark.asyncio
async def test_preview_without_reset_or_move() -> None:
    effect = await _preview({"instance_id": "lab01", "move_state_files": False}, _check())
    assert effect is not None
    kinds = {child["kind"] for child in effect["blast_radius"]["children"]}
    assert kinds == {"vm"}


@pytest.mark.asyncio
async def test_preview_unknown_instance_deletes_nothing() -> None:
    effect = await _preview({"instance_id": "gone01"}, _check(instance=[]))
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["children"] == []
    assert effect["blast_radius"]["object"]["found"] is False
    assert effect["blast_radius"]["object"]["resume"] is False
    assert effect["reset_router"] is False


@pytest.mark.asyncio
async def test_preview_refuses_a_listed_instance_without_vm_names() -> None:
    bare = {"InstanceID": "lab01", "Status": "Completed"}
    with pytest.raises(ValueError, match="cannot show what it would delete"):
        await _preview({"instance_id": "lab01"}, _check(instance=bare, state=[]))


@pytest.mark.asyncio
async def test_preview_lists_vms_from_the_state_file() -> None:
    bare = {"InstanceID": "lab01", "Status": "Completed"}
    state = [{"Hosts": [{"Name": "lab01-esx-01a"}], "Target": {"password": _SECRET}}]
    effect = await _preview({"instance_id": "lab01"}, _check(instance=bare, state=state))
    assert effect is not None
    vms = [child for child in effect["blast_radius"]["children"] if child["kind"] == "vm"]
    assert vms == [{"kind": "vm", "name": "lab01-esx-01a", "location": None}]
    assert _SECRET not in json.dumps(effect)


@pytest.mark.asyncio
async def test_preview_of_a_resume_lists_only_the_files() -> None:
    effect = await _preview(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=None),
        record=_interrupted_record(),
        tmux_alive=False,
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    radius = effect["blast_radius"]
    assert radius["object"] == {
        "kind": "holodeck_instance",
        "instance_id": "lab01",
        "found": False,
        "resume": True,
    }
    assert [child["kind"] for child in radius["children"]] == ["file_move", "file_move"]
    assert effect["reset_router"] is False
    assert "deletes no VM" in effect["note"]


@pytest.mark.asyncio
async def test_preview_of_a_resume_with_the_instance_still_listed() -> None:
    effect = await _preview(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=_listed("2026-01-01T09:00:00Z")),
        record=_interrupted_record(),
        tmux_alive=False,
    )
    assert effect is not None
    radius = effect["blast_radius"]
    assert radius["object"]["found"] is True
    assert radius["object"]["resume"] is True
    assert [child["kind"] for child in radius["children"]] == ["file_move", "file_move"]
    assert effect["reset_router"] is False
    assert "deletes no VM" in effect["note"]


@pytest.mark.asyncio
async def test_preview_deletes_again_when_the_delete_is_not_confirmed() -> None:
    effect = await _preview(
        {"instance_id": "lab01", "reset_router": True},
        _check(instance=_listed("2026-01-01T09:00:00Z")),
        record=_interrupted_record(deleted=None),
        tmux_alive=False,
    )
    assert effect is not None
    radius = effect["blast_radius"]
    assert radius["object"]["resume"] is False
    kinds = [child["kind"] for child in radius["children"]]
    assert kinds.count("vm") == 3
    assert "router_reset" in kinds
    assert effect["reset_router"] is True


@pytest.mark.asyncio
async def test_preview_older_toolkit_raises_version_message() -> None:
    with pytest.raises(ValueError, match=r"9\.0\.1"):
        await _preview({"instance_id": "lab01"}, _check(has_remove=False))

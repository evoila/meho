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

import meho_backplane.connectors.holodeck  # noqa: F401 -- registry side-effects
from meho_backplane.connectors._shared.pwsh import PwshRunError
from meho_backplane.connectors.holodeck import HOLODECK_OPS, HolodeckConnector
from meho_backplane.connectors.holodeck.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.holodeck.ops_remove import (
    REMOVE_OPS,
    _instance_remove_preview,
    file_name_pattern,
    instance_vms,
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
_SECRET = "not-a-real-password-123"


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


async def _remove(params: dict[str, Any], check: Any, *, exit_status: int = 0) -> tuple[Any, Any]:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
    ):
        if isinstance(check, Exception):
            mock_pwsh.side_effect = check
        else:
            mock_pwsh.return_value = check
        mock_cmd.return_value = _proc(stderr="duplicate session", exit_status=exit_status)
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
    mock_cmd.assert_not_called()


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
    assert instance_vms(None, "lab01") == []
    assert instance_vms([{"Name": "other-esx-01a"}], "lab01") == []


# ---------------------------------------------------------------------------
# Status: leftover_vms
# ---------------------------------------------------------------------------


async def _status(record: dict[str, Any] | None, pwsh: Any) -> dict[str, Any]:
    connector = HolodeckConnector()
    stdout = json.dumps(record) if record is not None else ""
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
    assert "cat /holodeck-runtime/logs/lab01-remove.json" in mock_cmd.call_args.args[1]
    return result


@pytest.mark.asyncio
async def test_status_reports_removed_and_leftover_vms() -> None:
    record = {
        "instance_id": "lab01",
        "state": "done",
        "started_at": "2026-01-01T10:00:00Z",
        "finished_at": "2026-01-01T10:20:00Z",
        "reset_router": False,
        "error": None,
        "leftover_vms": ["lab01-vcfinstaller-a"],
        "leftover_error": None,
        "moved_to": "/holodeck-runtime/removed/lab01-20260101-102000",
        "moved_files": ["/holodeck-runtime/state/lab01-state.json"],
    }
    gone = PwshRunError("instance not found", exit_status=1, stderr="")
    result = await _status(record, gone)
    assert result["status"] == "removed"
    assert result["leftover_vms"] == ["lab01-vcfinstaller-a"]
    assert result["removal"]["moved_files"] == ["/holodeck-runtime/state/lab01-state.json"]
    assert "error" not in result
    assert any("vmware.composite.vm.destroy" in note for note in result["notes"])


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


@pytest.mark.asyncio
async def test_preview_lists_vms_router_reset_and_files() -> None:
    connector = HolodeckConnector()
    with patch(_PWSH, new_callable=AsyncMock, return_value=_check()):
        effect = await _instance_remove_preview(
            _ctx(connector, {"instance_id": "lab01", "reset_router": True})
        )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    radius = effect["blast_radius"]
    assert radius["object"] == {
        "kind": "holodeck_instance",
        "instance_id": "lab01",
        "found": True,
    }
    assert radius["irreversibility"] == "permanent"
    kinds = [child["kind"] for child in radius["children"]]
    assert kinds.count("vm") == 3
    assert "router_reset" in kinds
    assert kinds.count("file_move") == 2
    assert _SECRET not in json.dumps(effect)


@pytest.mark.asyncio
async def test_preview_without_reset_or_move() -> None:
    connector = HolodeckConnector()
    with patch(_PWSH, new_callable=AsyncMock, return_value=_check()):
        effect = await _instance_remove_preview(
            _ctx(connector, {"instance_id": "lab01", "move_state_files": False})
        )
    assert effect is not None
    kinds = {child["kind"] for child in effect["blast_radius"]["children"]}
    assert kinds == {"vm"}


@pytest.mark.asyncio
async def test_preview_unknown_instance_deletes_nothing() -> None:
    connector = HolodeckConnector()
    with patch(_PWSH, new_callable=AsyncMock, return_value=_check(instance=[])):
        effect = await _instance_remove_preview(_ctx(connector, {"instance_id": "gone01"}))
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["children"] == []
    assert effect["blast_radius"]["object"]["found"] is False


@pytest.mark.asyncio
async def test_preview_older_toolkit_raises_version_message() -> None:
    connector = HolodeckConnector()
    with (
        patch(_PWSH, new_callable=AsyncMock, return_value=_check(has_remove=False)),
        pytest.raises(ValueError, match=r"9\.0\.1"),
    ):
        await _instance_remove_preview(_ctx(connector, {"instance_id": "lab01"}))

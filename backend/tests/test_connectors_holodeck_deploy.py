# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# code-quality-allow: a per-op acceptance-criteria matrix (registration /
# depot-refusal / precheck / status-envelope / idempotency / op-identity /
# secret-hygiene) for four deploy ops; the length is independent test cases,
# not logic complexity. Mirrors test_connectors_holodeck_write.py.

"""Tests for the Holodeck deploy-lifecycle typed ops (#2908, Initiative #2907).

Acceptance-criteria coverage matrix:

* Registration: 4 ops via the two-phase typed registration (register_connector_v2
  at import + register_typed_operation at register_operations), JSON-Schema
  params, curated group when_to_use, llm_instructions.
* ``config.apply``: depot params refused for a VCF 5.2.x version by the real
  framework ``validate_params`` (schema if/then) AND the direct semantic helper;
  New-HoloDeckConfig composed WITHOUT ``-Default``.
* ``instance.start``: the single-shot fail-fast Connect-VIServer precheck runs
  BEFORE launch, and a locked/failed precheck blocks the launch entirely.
* ``instance.status``: the structured envelope covers running/completed/failed
  and re-maps the benign VCF 5.2.x Sync-HolodeckComponents "No route to host"
  terminal quirk to completed + a note (usable as an OperationCallVerify target).
* ``router.patch``: idempotent verify-then-apply (second run -> all
  already_applied) + the 9.1 verify-only path (greps, never seds).
* Uniform op-identity envelope (#2681) on all four (parked proposed_effect for
  the three gated ops; op_id on the safe op's result).
* Secret hygiene (#1503): router.patch parks op-identity-only (no token) and is
  pinned credential-class; config.apply's schema admits no plaintext credential.

Tests mirror the windows_dns / holodeck mocked-pwsh transport pattern
(test_connectors_holodeck_write.py): pure helpers unit-tested directly, handler
paths driven with a mocked ``pwsh_run`` / ``_run_command``, and a DB-backed
dispatch harness for the park + op-identity + audit surface.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest

import meho_backplane.connectors.holodeck  # noqa: F401 -- registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.holodeck import HolodeckConnector
from meho_backplane.connectors.holodeck._pwsh import PwshRunError
from meho_backplane.connectors.holodeck.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.holodeck.ops_deploy import (
    DEPLOY_OPS,
    HOLODECK_DEPLOY_CREDENTIAL_OPS,
    ROUTER_PATCHES,
    map_instance_status_envelope,
    target_hr_version,
    validate_config_apply_params,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str = "holorouter-deploy-test"
    host: str = "holorouter.test.invalid"
    port: int | None = 22
    secret_ref: str = "meho/testing/holodeck/holorouter-deploy-test"
    version: str | None = "9.0.2.0"
    fingerprint: dict[str, Any] = field(default_factory=dict)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _op(op_id: str) -> Any:
    return next(op for op in DEPLOY_OPS if op.op_id == op_id)


EXPECTED_DEPLOY_OP_IDS: frozenset[str] = frozenset(
    {
        "holodeck.config.apply",
        "holodeck.instance.start",
        "holodeck.instance.status",
        "holodeck.router.patch",
    }
)


# ===========================================================================
# A. Registration contract (AC1, AC6)
# ===========================================================================


def test_deploy_ops_registration_set() -> None:
    assert {op.op_id for op in DEPLOY_OPS} == EXPECTED_DEPLOY_OP_IDS
    assert len(DEPLOY_OPS) == 4


def test_deploy_ops_safety_and_approval() -> None:
    expected = {
        "holodeck.config.apply": ("dangerous", True),
        "holodeck.instance.start": ("dangerous", True),
        "holodeck.instance.status": ("safe", False),
        "holodeck.router.patch": ("dangerous", True),
    }
    for op in DEPLOY_OPS:
        assert (op.safety_level, op.requires_approval) == expected[op.op_id], op.op_id


def test_deploy_ops_carry_transport_note_and_instructions() -> None:
    for op in DEPLOY_OPS:
        instr = op.llm_instructions or {}
        assert "Holodeck has no REST API" in instr.get("when_to_use", ""), op.op_id
        assert instr.get("output_shape"), f"{op.op_id} missing output_shape"


def test_deploy_ops_group_keys_are_curated() -> None:
    """Every deploy op's group_key has a curated when_to_use on the connector.

    The connector's register_operations walk fails closed with ValueError when
    a group_key lacks a curated blurb; this asserts the merge landed.
    """
    for op in DEPLOY_OPS:
        assert op.group_key in _WHEN_TO_USE_BY_GROUP, op.group_key
        assert _WHEN_TO_USE_BY_GROUP[op.group_key].strip()


def test_deploy_ops_handlers_resolve_on_connector() -> None:
    for op in DEPLOY_OPS:
        assert getattr(HolodeckConnector, op.handler_attr, None) is not None, op.handler_attr


# ===========================================================================
# B. config.apply depot refusal for 5.2.x (AC2)
# ===========================================================================

_CONFIG_SCHEMA = _op("holodeck.config.apply").parameter_schema
_DEPOT = {"host": "depot.lab", "port": 8443, "protocol": "https", "credential_ref": "secret/depot"}


@pytest.mark.parametrize("version", ["5.2", "5.2.1", "5.2.2"])
def test_config_apply_validate_params_rejects_depot_on_5_2(version: str) -> None:
    """The real framework validate_params (schema if/then) rejects depot on 5.2.x."""
    errors = validate_params(
        _CONFIG_SCHEMA, {"version": version, "variant": "full", "depot": _DEPOT}
    )
    assert errors, f"depot must be refused for {version}"


@pytest.mark.parametrize("version", ["9.0.2.0", "9.1.0.0"])
def test_config_apply_validate_params_accepts_depot_on_9_x(version: str) -> None:
    errors = validate_params(
        _CONFIG_SCHEMA, {"version": version, "variant": "full", "depot": _DEPOT}
    )
    assert errors == [], (version, errors)


def test_config_apply_validate_params_accepts_5_2_without_depot() -> None:
    errors = validate_params(_CONFIG_SCHEMA, {"version": "5.2.1", "variant": "management_only"})
    assert errors == []


def test_config_apply_schema_admits_no_plaintext_credential() -> None:
    """Secret hygiene: config.apply's schema has no plaintext credential field.

    additionalProperties=False + the absence of any password/token property
    means a caller CANNOT pass a plaintext deploy-target credential as a param.
    The only credential reference is depot.credential_ref -- a Vault path.
    """
    props = _CONFIG_SCHEMA["properties"]
    for name in props:
        assert "password" not in name.lower(), name
        assert name.lower() != "token", name
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"version": "9.0.2.0", "variant": "full", "password": "hunter2"}, _CONFIG_SCHEMA
        )
    # depot carries a Vault *ref*, never a plaintext password field.
    depot_props = _CONFIG_SCHEMA["properties"]["depot"]["properties"]
    assert "credential_ref" in depot_props
    assert "password" not in depot_props


def test_validate_config_apply_params_direct() -> None:
    assert validate_config_apply_params({"version": "5.2.1", "variant": "full", "depot": _DEPOT})
    assert (
        validate_config_apply_params({"version": "9.0.2.0", "variant": "full", "depot": _DEPOT})
        == []
    )
    assert validate_config_apply_params({"version": "5.2.1", "variant": "full"}) == []


# ===========================================================================
# C. config.apply handler (mocked pwsh) -- never -Default (AC "Outcome")
# ===========================================================================


@pytest.mark.asyncio
async def test_config_apply_composes_new_holodeckconfig_without_default() -> None:
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.return_value = {"configID": "hd9mgmt1", "configWritten": True}
        result = await connector.config_apply(
            _StubTarget(),
            {
                "version": "9.0.2.0",
                "variant": "management_only",
                "external_ip": "vcenter.example.lab",
                "depot": _DEPOT,
            },
        )
    assert result["applied"] is True
    assert result["config_id"] == "hd9mgmt1"
    assert result["deploy"]["version"] == "9.0.2.0"
    script = mock_pwsh.await_args.args[2]
    assert "New-HoloDeckConfig" in script
    assert "-Default" not in script, "New-HoloDeckConfig must NEVER use -Default"
    assert "Import-HoloDeckConfig" in script
    # Credentials referenced by env-var NAME only -- no interpolated value.
    assert "$env:VC_USER" in script and "$env:VC_PW" in script
    # #3081: the warning stream is silenced at the source and the write is
    # verified on disk before the op can report success.
    assert "$Global:WarningPreference = 'SilentlyContinue'" in script
    assert "Test-Path" in script and "/holodeck-runtime/config/" in script


# ===========================================================================
# C.1 config.apply fail-closed on an unwritten config (#3081)
# ===========================================================================


@pytest.mark.asyncio
async def test_config_apply_fails_closed_on_null_config_id() -> None:
    """A cmdlet that returns configID:null must NOT report applied:true (#3081).

    The severe defect: config.apply reported ``{applied: true, config_id:
    null}`` while writing nothing to the HoloRouter. The self-contradictory
    envelope is now impossible -- a null configID fails closed.
    """
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.return_value = {"configID": None, "configWritten": False}
        result = await connector.config_apply(
            _StubTarget(), {"version": "9.0.2.0", "variant": "management_only"}
        )
    assert result["applied"] is False
    assert result["config_id"] is None
    assert "NOT applied" in result["error"]


@pytest.mark.asyncio
async def test_config_apply_fails_closed_when_file_not_on_disk() -> None:
    """A real-looking configID with no persisted file must fail closed (#3081).

    verify-after-write: even when New-HoloDeckConfig returns a plausible
    configID, applied stays false unless the config file is actually on disk.
    """
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.return_value = {"configID": "hd9mgmt1", "configWritten": False}
        result = await connector.config_apply(
            _StubTarget(), {"version": "9.0.2.0", "variant": "management_only"}
        )
    assert result["applied"] is False
    # The configID is still surfaced for triage, but applied is false.
    assert result["config_id"] == "hd9mgmt1"
    assert "NOT applied" in result["error"]


@pytest.mark.asyncio
async def test_config_apply_pwsh_error_fails_closed() -> None:
    """A PwshRunError (empty/corrupt stdout) surfaces as applied:false (#3081).

    ``holodeck.config.show`` already fails honestly on the same transport;
    config.apply must too, rather than defaulting to success.
    """
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.side_effect = PwshRunError(
            "pwsh -EncodedCommand produced empty stdout; expected ConvertTo-Json output",
            exit_status=0,
            stderr="",
        )
        result = await connector.config_apply(
            _StubTarget(), {"version": "9.0.2.0", "variant": "management_only"}
        )
    assert result["applied"] is False
    assert "empty stdout" in result["error"]


# ===========================================================================
# C.2 config.apply end-to-end through the real pwsh transport (#3081)
# ===========================================================================
#
# These drive the REAL pwsh_run by stubbing the SSH exec channel
# (connector._run_command) instead of mocking pwsh_run, so the CLIXML strip
# + empty-stdout guard + fail-closed handler are exercised as one chain.

_APPLY_PARAMS = {"version": "9.0.2.0", "variant": "management_only", "external_ip": "vc.lab"}
_CLEAN_APPLY_JSON = '{"configID":"hd9mgmt1","configWritten":true}'
_CLIXML_WARNING = (
    "#< CLIXML\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<S S="warning">The names of some imported commands from the module '
    "'HoloDeck' include unapproved verbs that might make them less "
    "discoverable.</S></Objs>\n"
)


@pytest.mark.asyncio
async def test_config_apply_transport_parses_clixml_polluted_stdout() -> None:
    """(a) CLIXML warning prepended to valid JSON -> stripped -> applied:true."""
    connector = HolodeckConnector()
    polluted = _CLIXML_WARNING + _CLEAN_APPLY_JSON
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _proc(stdout=polluted, exit_status=0)
        result = await connector.config_apply(_StubTarget(), dict(_APPLY_PARAMS))
    assert result["applied"] is True
    assert result["config_id"] == "hd9mgmt1"


@pytest.mark.asyncio
async def test_config_apply_transport_fails_closed_on_empty_stdout() -> None:
    """(b) empty stdout -> PwshRunError -> applied:false, never a false success."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _proc(stdout="", exit_status=0)
        result = await connector.config_apply(_StubTarget(), dict(_APPLY_PARAMS))
    assert result["applied"] is False
    assert "empty stdout" in result["error"]


@pytest.mark.asyncio
async def test_config_apply_transport_succeeds_on_clean_json() -> None:
    """(c) clean JSON -> applied:true with the parsed config_id."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _proc(stdout=_CLEAN_APPLY_JSON, exit_status=0)
        result = await connector.config_apply(_StubTarget(), dict(_APPLY_PARAMS))
    assert result["applied"] is True
    assert result["config_id"] == "hd9mgmt1"


@pytest.mark.asyncio
async def test_config_apply_transport_never_true_with_null_config_id() -> None:
    """The invariant: CLIXML-only stdout (no JSON) can never yield applied:true.

    A fresh HoloRouter where the write silently failed emits only the warning
    block; stripped, that is empty stdout -> fail-closed. This is the exact
    #3081 scenario end-to-end.
    """
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = _proc(stdout=_CLIXML_WARNING, exit_status=0)
        result = await connector.config_apply(_StubTarget(), dict(_APPLY_PARAMS))
    assert result["applied"] is False
    assert not (result["applied"] is True and result["config_id"] is None)


@pytest.mark.asyncio
async def test_config_apply_handler_rejects_depot_on_5_2_belt_and_braces() -> None:
    """A direct handler call bypassing schema validation still refuses depot on 5.2.x."""
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        result = await connector.config_apply(
            _StubTarget(), {"version": "5.2.1", "variant": "full", "depot": _DEPOT}
        )
        mock_pwsh.assert_not_awaited()
    assert result["applied"] is False
    assert "Cloud Builder" in result["error"]


# ===========================================================================
# D. instance.start fail-fast precheck (AC2 second half)
# ===========================================================================

_LOCKED_ACCOUNT = {
    "ok": False,
    "message": "Cannot complete login: incorrect user name or password; account is locked.",
}
_START_PARAMS = {"config_id": "hd9mgmt1", "instance_id": "hd9mgmt", "version": "9.0.2.0"}


@pytest.mark.asyncio
async def test_instance_start_precheck_failure_blocks_launch() -> None:
    """A locked/failed Connect-VIServer precheck blocks the launch entirely.

    The precheck runs via pwsh_run; the launch runs via _run_command. On a
    failed precheck the handler returns started=False and _run_command (the
    launch) is NEVER awaited -- the retry-storm never fires against the bad
    credential.
    """
    connector = HolodeckConnector()
    with (
        patch(
            "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
            new_callable=AsyncMock,
        ) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_launch,
    ):
        mock_pwsh.return_value = _LOCKED_ACCOUNT
        result = await connector.instance_start(_StubTarget(), _START_PARAMS)
        mock_launch.assert_not_awaited()
    assert result["started"] is False
    assert result["precheck"]["ok"] is False
    assert "not launched" in result["error"]


@pytest.mark.asyncio
async def test_instance_start_happy_path_launches_detached() -> None:
    connector = HolodeckConnector()
    with (
        patch(
            "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
            new_callable=AsyncMock,
        ) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_launch,
    ):
        mock_pwsh.return_value = {"ok": True, "server": "vcenter.example.lab"}
        mock_launch.return_value = _proc(stdout="")
        result = await connector.instance_start(_StubTarget(), _START_PARAMS)
    assert result["started"] is True
    assert result["detached"] is True
    assert result["precheck"]["ok"] is True
    assert result["start_marker"]["tmux_session"] == "holodeck-deploy-hd9mgmt"
    launch_cmd = mock_launch.await_args.args[1]
    assert launch_cmd.startswith("tmux new-session -d")


@pytest.mark.asyncio
async def test_instance_start_precheck_pwsh_error_is_fail_fast() -> None:
    """A pwsh process failure in the precheck also blocks the launch."""
    connector = HolodeckConnector()
    with (
        patch(
            "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
            new_callable=AsyncMock,
        ) as mock_pwsh,
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_launch,
    ):
        mock_pwsh.side_effect = PwshRunError("precheck blew up", exit_status=1, stderr="")
        result = await connector.instance_start(_StubTarget(), _START_PARAMS)
        mock_launch.assert_not_awaited()
    assert result["started"] is False


# ===========================================================================
# E. instance.status envelope + benign 5.2 quirk (AC3)
# ===========================================================================


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("InProgress", "running"),
        ("Running", "running"),
        ("Completed", "completed"),
        ("Succeeded", "completed"),
        ("Failed", "failed"),
        ("COMPLETED_WITH_FAILURE", "failed"),
        ("weird-token", "unknown"),
    ],
)
def test_map_status_envelope_normalises_status(raw_status: str, expected: str) -> None:
    envelope = map_instance_status_envelope(
        {"Status": raw_status, "CurrentStep": "Deploy-VcfInstaller"}
    )
    assert envelope["status"] == expected
    assert envelope["current_step"] == "Deploy-VcfInstaller"
    assert envelope["notes"] == []


def test_map_status_envelope_flat_fields_for_verify_and_sensor() -> None:
    """The flat status/current_step fields are the OperationCallVerify/Sensor target."""
    envelope = map_instance_status_envelope(
        {
            "Status": "InProgress",
            "CurrentStep": "Install-VcfInstallerBundles",
            "StartTime": "2026-08-17T10:00:00Z",
            "LastUpdate": "2026-08-17T10:12:00Z",
            "InstanceID": "hd9mgmt",
        }
    )
    assert envelope == {
        "status": "running",
        "current_step": "Install-VcfInstallerBundles",
        "started_at": "2026-08-17T10:00:00Z",
        "updated_at": "2026-08-17T10:12:00Z",
        "instance_id": "hd9mgmt",
        "notes": [],
    }


def test_map_status_envelope_benign_5_2_sync_quirk_is_completed() -> None:
    """The 5.2.x Sync-HolodeckComponents 'No route to host' terminal quirk -> completed + note."""
    envelope = map_instance_status_envelope(
        {
            "Status": "COMPLETED_WITH_FAILURE",
            "CurrentStep": "Sync-HolodeckComponents",
            "Message": "java.net.NoRouteToHostException: No route to host (vc-mgmt-a...)",
            "Version": "5.2.1",
        }
    )
    assert envelope["status"] == "completed"
    assert len(envelope["notes"]) == 1
    assert "5.2" in envelope["notes"][0] and "not a failure" in envelope["notes"][0]


def test_map_status_envelope_real_9_x_failure_stays_failed() -> None:
    """A genuine 9.x failure (not the Sync/no-route quirk) stays failed -- no note."""
    envelope = map_instance_status_envelope(
        {
            "Status": "Failed",
            "CurrentStep": "Deploy-VcfInstaller",
            "Message": "duplicate VM name",
            "Version": "9.0.2.0",
        }
    )
    assert envelope["status"] == "failed"
    assert envelope["notes"] == []


def test_map_status_envelope_list_shape_uses_first() -> None:
    envelope = map_instance_status_envelope([{"Status": "Completed"}, {"Status": "Failed"}])
    assert envelope["status"] == "completed"


@pytest.mark.asyncio
async def test_instance_status_handler_maps_running() -> None:
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.return_value = {"Status": "InProgress", "CurrentStep": "ESXBuild"}
        result = await connector.instance_status(_StubTarget(), {})
    assert result["status"] == "running"
    assert result["current_step"] == "ESXBuild"


@pytest.mark.asyncio
async def test_instance_status_handler_pwsh_error_is_unknown() -> None:
    connector = HolodeckConnector()
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.side_effect = PwshRunError("no instance", exit_status=1, stderr="")
        result = await connector.instance_status(_StubTarget(), {})
    assert result["status"] == "unknown"
    assert "error" in result


# ===========================================================================
# F. router.patch idempotency + 9.1 verify-only (AC4)
# ===========================================================================


class _FakePatchFS:
    """A stateful fake HoloRouter module tree for router.patch idempotency tests.

    Tracks per-file applied state. A grep for the applied marker returns the
    expected count iff the patch has been applied; a grep for the unpatched
    marker returns 1 iff not yet applied; a ``sed -i`` flips the file to
    applied. The ``sed -i`` check runs first because a patch's sed command
    embeds BOTH markers (pattern + replacement).
    """

    def __init__(self, *, all_applied: bool) -> None:
        self.applied: dict[str, bool] = {p.file_path: all_applied for p in ROUTER_PATCHES}
        self.sed_calls: list[str] = []

    async def run(
        self, target: Any, cmd: str, *, operator: Any = None, timeout: float = 30.0
    ) -> Any:
        patch_spec = next((p for p in ROUTER_PATCHES if p.file_path in cmd), None)
        if patch_spec is None:
            return _proc(stdout="0")
        if "sed -i" in cmd:
            self.sed_calls.append(cmd)
            self.applied[patch_spec.file_path] = True
            return _proc(stdout="")
        if patch_spec.applied_marker in cmd:
            count = patch_spec.applied_count if self.applied[patch_spec.file_path] else 0
            return _proc(stdout=str(count))
        if patch_spec.unpatched_marker in cmd:
            return _proc(stdout="0" if self.applied[patch_spec.file_path] else "1")
        return _proc(stdout="0")


def _states(result: dict[str, Any]) -> dict[str, str]:
    return {p["patch_id"]: p["state"] for p in result["patches"]}


@pytest.mark.asyncio
async def test_router_patch_applies_then_second_run_is_idempotent() -> None:
    """First run applies all three unpatched patches; the second run -> all already_applied."""
    connector = HolodeckConnector()
    fs = _FakePatchFS(all_applied=False)
    target = _StubTarget(version="9.0.2.0")

    with patch.object(connector, "_run_command", side_effect=fs.run):
        first = await connector.router_patch(target, {})
        assert _states(first) == {"C1": "applied", "C2": "applied", "C3": "applied"}
        assert first["patched"] is True
        assert first["verify_only"] is False
        assert len(fs.sed_calls) == 3

        second = await connector.router_patch(target, {})
    assert _states(second) == {
        "C1": "already_applied",
        "C2": "already_applied",
        "C3": "already_applied",
    }
    assert second["patched"] is False
    assert len(fs.sed_calls) == 3, "the idempotent second run must run no new sed"


@pytest.mark.asyncio
async def test_router_patch_9_1_is_verify_only_never_seds() -> None:
    """On a 9.1 HoloRouter the patches are fixed upstream -> verify-only, no sed."""
    connector = HolodeckConnector()
    fs = _FakePatchFS(all_applied=True)
    target = _StubTarget(version="9.1.0.0912")

    with patch.object(connector, "_run_command", side_effect=fs.run):
        result = await connector.router_patch(target, {})
    assert result["verify_only"] is True
    assert _states(result) == {
        "C1": "already_applied",
        "C2": "already_applied",
        "C3": "already_applied",
    }
    assert fs.sed_calls == [], "9.1 verify-only must never sed"


@pytest.mark.asyncio
async def test_router_patch_9_1_marker_absent_is_not_applicable_no_sed() -> None:
    """9.1 with the applied marker absent reports not_applicable, still never seds."""
    connector = HolodeckConnector()
    fs = _FakePatchFS(all_applied=False)  # applied marker absent on this fake
    target = _StubTarget(version="9.1.0.0912")

    with patch.object(connector, "_run_command", side_effect=fs.run):
        result = await connector.router_patch(target, {})
    assert result["verify_only"] is True
    assert set(_states(result).values()) == {"not_applicable"}
    assert fs.sed_calls == []


def test_target_hr_version_prefers_version_then_fingerprint() -> None:
    assert target_hr_version(_StubTarget(version="9.1.0")) == "9.1.0"
    assert target_hr_version(_StubTarget(version=None, fingerprint={"version": "9.0.2"})) == "9.0.2"
    assert target_hr_version(_StubTarget(version=None, fingerprint={})) is None


# ===========================================================================
# G. DB-backed dispatch: two-phase registration, op-identity envelope (#2681),
#    secret hygiene (#1503) -- AC1 / AC5
# ===========================================================================

_CONNECTOR_ID = "holodeck-ssh-9.0"
_TARGET_NAME = "holodeck-deploy-e2e"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000029a8")
_OPERATOR = Operator(
    sub="holodeck-deploy-e2e-test",
    name="Holodeck Deploy E2E Operator",
    email=None,
    raw_jwt="<holodeck-deploy-e2e-raw-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

# Non-secret park params per gated op (all pass schema validation).
_PARK_PARAMS: dict[str, dict[str, Any]] = {
    "holodeck.config.apply": {
        "version": "9.0.2.0",
        "variant": "management_only",
        "external_ip": "vcenter.example.lab",
        "depot": {"host": "depot.lab", "credential_ref": "secret/holodeck/depot"},
    },
    "holodeck.instance.start": {
        "config_id": "hd9mgmt1",
        "instance_id": "hd9mgmt",
        "version": "9.0.2.0",
    },
    "holodeck.router.patch": {},
}
_EXPECTED_SAFETY = {
    "holodeck.config.apply": "dangerous",
    "holodeck.instance.start": "dangerous",
    "holodeck.router.patch": "dangerous",
}
_ENVELOPE_IDENTITY_KEYS = frozenset(
    {"op_id", "connector_id", "target_id", "op_class", "safety_level"}
)


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    from meho_backplane.operations import reset_dispatcher_caches

    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


async def _seed_target() -> None:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="holodeck",
                host="holorouter.e2e.invalid",
                port=22,
                fqdn=None,
                secret_ref="kv/dev/holodeck/deploy-e2e",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0.2.0"},
                version="9.0.2.0",
                notes="seeded by test_connectors_holodeck_deploy",
            )
        )
        await session.commit()


@pytest.fixture
async def holodeck_deploy_e2e() -> AsyncIterator[HolodeckConnector]:
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
    from meho_backplane.operations.dispatcher import set_default_reducer
    from meho_backplane.operations.reducer import PassThroughReducer

    set_default_reducer(PassThroughReducer())
    await HolodeckConnector.register_operations()
    await _seed_target()
    connector = HolodeckConnector()
    _CONNECTOR_INSTANCE_CACHE[HolodeckConnector] = connector  # type: ignore[assignment]
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _parked_effect(approval_request_id: str) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.approval_queue import get_request

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        request = await get_request(
            session, tenant_id=_TENANT_ID, request_id=uuid.UUID(approval_request_id)
        )
        return dict(request.proposed_effect)


@pytest.mark.asyncio
async def test_two_phase_registration_lands_all_four_descriptors(
    holodeck_deploy_e2e: HolodeckConnector,
) -> None:
    """register_operations (phase 2) upserts endpoint_descriptor rows for all four ops."""
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.product == "holodeck",
                EndpointDescriptor.op_id.in_(tuple(EXPECTED_DEPLOY_OP_IDS)),
            )
        )
        found = {r for (r,) in rows.all()}
    assert found == EXPECTED_DEPLOY_OP_IDS


@pytest.mark.asyncio
async def test_gated_ops_carry_uniform_op_identity_envelope(
    holodeck_deploy_e2e: HolodeckConnector,
) -> None:
    """#2681: every parked gated op carries the uniform op-identity + metadata envelope."""
    identity_sets: list[frozenset[str]] = []
    for op_id, params in _PARK_PARAMS.items():
        result = await _dispatch(op_id, params, approved=False)
        assert result["status"] == "awaiting_approval", (op_id, result)
        effect = await _parked_effect(result["extras"]["approval_request_id"])
        assert effect["op_id"] == op_id
        assert effect["connector_id"] == _CONNECTOR_ID
        assert effect["op_class"] == classify_op(op_id)
        assert effect["safety_level"] == _EXPECTED_SAFETY[op_id]
        present = _ENVELOPE_IDENTITY_KEYS & frozenset(effect)
        identity_sets.append(present)
        assert present == _ENVELOPE_IDENTITY_KEYS, (op_id, sorted(effect))
    assert all(s == identity_sets[0] for s in identity_sets)


@pytest.mark.asyncio
async def test_router_patch_parks_op_identity_only_no_token(
    holodeck_deploy_e2e: HolodeckConnector,
) -> None:
    """Secret hygiene: router.patch is credential-class -> parked op-identity-only, no token."""
    assert classify_op("holodeck.router.patch") == "credential_write"
    assert set(HOLODECK_DEPLOY_CREDENTIAL_OPS) == {"holodeck.router.patch"}
    result = await _dispatch("holodeck.router.patch", {}, approved=False)
    assert result["status"] == "awaiting_approval"
    effect = await _parked_effect(result["extras"]["approval_request_id"])
    # Credential-class + no bespoke builder -> params-echo suppressed.
    assert effect["preview_populated"] is False
    assert "params_echo" not in effect and "preview" not in effect
    blob = json.dumps(effect).lower()
    assert "brcm_build_token" not in blob
    assert "broadcombuildtoken" not in blob


@pytest.mark.asyncio
async def test_config_apply_parks_with_param_echo_no_secret_value(
    holodeck_deploy_e2e: HolodeckConnector,
) -> None:
    """config.apply keeps its param-echo preview and leaks no credential value."""
    assert classify_op("holodeck.config.apply") != "credential_write"
    result = await _dispatch(
        "holodeck.config.apply", _PARK_PARAMS["holodeck.config.apply"], approved=False
    )
    assert result["status"] == "awaiting_approval"
    effect = await _parked_effect(result["extras"]["approval_request_id"])
    # Param-echo preview is populated (the issue's "param-echo preview").
    assert effect["preview_populated"] is True
    assert "params_echo" in effect
    # No plaintext credential value exists in the params, so none can leak.
    blob = json.dumps(effect)
    assert "hunter2" not in blob and "VMware123" not in blob


@pytest.mark.asyncio
async def test_instance_status_result_carries_op_identity(
    holodeck_deploy_e2e: HolodeckConnector,
) -> None:
    """The safe op's result envelope carries its op-identity (op_id) -- #2681 for all four."""
    with patch(
        "meho_backplane.connectors.holodeck.ops_deploy.pwsh_run",
        new_callable=AsyncMock,
    ) as mock_pwsh:
        mock_pwsh.return_value = {"Status": "InProgress", "CurrentStep": "ESXBuild"}
        result = await _dispatch("holodeck.instance.status", {}, approved=False)
    assert result["status"] == "ok", result.get("error")
    assert result["op_id"] == "holodeck.instance.status"
    assert result["result"]["status"] == "running"

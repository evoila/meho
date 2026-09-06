# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the linux-ssh governed write surface (T2).

Coverage matrix (per the Task acceptance criteria):

* ``WRITE_OPS`` registration shape -- five ops with the correct tier table:
  ``service.control`` ``caution`` / no approval; ``file.write`` /
  ``script.run`` / ``sysctl.write`` / ``firewall.load`` ``dangerous`` +
  ``requires_approval=True``. No op is ``destructive``.
* Sudo wire-shape byte-identity with the rke2 reference + control-char reject
  before the connection is opened.
* Per-verb command construction + injection safety (pure builders): every
  operator value is base64-carried or ``shlex.quote``d, never spliced raw.
* ``file.write`` backup -> write -> validate -> rollback (a validate failure
  leaves the original in place); ``firewall.load`` validates before apply.
* ``script.run`` returns stdout/stderr/exit inline; large stdout is a line
  list that spills through the JSONFlux reducer.
* Broadcast-clamp membership: ``file.write`` / ``script.run`` classify
  ``credential_write``; ``sysctl.write`` / ``firewall.load`` /
  ``service.control`` do not.
* Park-time bespoke-preview redaction: no ``content`` / ``script`` /
  ``arguments`` / ``env`` value leaks.
* ``preview_operation`` returns ``preview_unavailable`` for the two
  credential-class writes with no secret in the envelope (mirrors fbbf536f);
  the two non-secret writes preview via the generic params-echo default.
* Dispatcher gate: each ``dangerous`` write parks; ``service.control``
  (caution) does not.
"""

from __future__ import annotations

import base64
import shlex
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import _CREDENTIAL_WRITE_OPS, classify_op
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux._sudo import build_sudo_bash_remote_cmd
from meho_backplane.connectors.linux.ops import PathConfinementError
from meho_backplane.connectors.linux.ops_write import (
    WRITE_OPS,
    LinuxWriteError,
    LinuxWriteSafetyError,
    _linux_file_write_preview,
    _linux_script_run_preview,
    bound_firewall_backend,
    bound_service_action,
    build_file_write_script,
    build_firewall_load_script,
    build_script_run_wrapper,
    build_service_control_script,
    build_sysctl_write_script,
    confine_write_path,
    linux_file_write,
    linux_firewall_load,
    linux_script_run,
    linux_service_control,
    linux_sysctl_write,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer, _detect_collection
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Canaries -- synthetic values that must never surface in a preview / result.
# Deliberately shaped to avoid the conftest SECRET_LEAK_PATTERNS denylist
# (no ``token=`` / ``secret=`` / ``password=`` / ``api_key=`` / ``Bearer``).
# ---------------------------------------------------------------------------

_CONTENT_CANARY = "MEHO_FILE_BODY_CANARY_DO_NOT_LEAK_zzq"  # gitleaks:allow -- synthetic
_SCRIPT_CANARY = "MEHO_SCRIPT_BODY_CANARY_DO_NOT_LEAK_zzq"  # gitleaks:allow -- synthetic
_ARG_CANARY = "--flag MEHO_ARG_CANARY_zzq"
_ENV_VALUE_CANARY = "MEHO_ENV_VALUE_CANARY_zzq"


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


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
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="linux-write-test",
    host="linux-host.test.invalid",
    port=22,
    secret_ref="meho/testing/linux/write-test",
)

_SUDO_CANARY = "linux-write-sudo-canary-xyz-991"  # gitleaks:allow -- synthetic


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _op(op_id: str) -> Any:
    return next(op for op in WRITE_OPS if op.op_id == op_id)


def _decode_b64_tokens(command: str) -> str:
    """Return the concatenated decode of every base64-looking token in *command*.

    The write builders base64-carry operator values; this decodes each quoted
    token so a test can assert the *decoded* body is what the builder embedded
    (and, by contrast, that the raw body never appears verbatim in the command).
    """
    out: list[str] = []
    for raw in command.replace("'", " ").replace('"', " ").split():
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception:
            continue
        if decoded:
            out.append(decoded.decode("utf-8", "replace"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


_EXPECTED_WRITE_OP_IDS: frozenset[str] = frozenset(
    {
        "linux.file.write",
        "linux.service.control",
        "linux.script.run",
        "linux.sysctl.write",
        "linux.firewall.load",
    }
)


def test_write_ops_registration_set() -> None:
    assert {op.op_id for op in WRITE_OPS} == _EXPECTED_WRITE_OP_IDS


def test_write_op_tiers() -> None:
    by_id = {op.op_id: op for op in WRITE_OPS}
    assert by_id["linux.service.control"].safety_level == "caution"
    assert by_id["linux.service.control"].requires_approval is False
    for op_id in _EXPECTED_WRITE_OP_IDS - {"linux.service.control"}:
        assert by_id[op_id].safety_level == "dangerous", op_id
        assert by_id[op_id].requires_approval is True, op_id
    # No write op is destructive (the destructive-builder gate does not apply).
    for op in WRITE_OPS:
        assert op.safety_level != "destructive", op.op_id


def test_write_ops_declare_no_secret_value_param() -> None:
    secret_fields = {"password", "passwd", "secret", "credential", "token", "api_key"}
    for op in WRITE_OPS:
        props = op.parameter_schema.get("properties", {})
        assert not (secret_fields & {k.lower() for k in props}), op.op_id


def test_write_ops_schemas_closed_and_have_ssh_note() -> None:
    for op in WRITE_OPS:
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id
        assert "SSH" in op.llm_instructions["when_to_use"], op.op_id


# ---------------------------------------------------------------------------
# Sudo wire-shape + control-char reject
# ---------------------------------------------------------------------------


def test_sudo_wire_shape_is_byte_identical_to_the_reference() -> None:
    """AC: the elevation wire-shape must not drift between connector families."""
    from meho_backplane.connectors.rke2._sudo import (
        build_sudo_bash_remote_cmd as rke2_build,
    )

    for n in (0, 1, 42, 65536):
        assert build_sudo_bash_remote_cmd(n) == rke2_build(n)
    assert build_sudo_bash_remote_cmd(7) == (
        'set -e; umask 077; f=$(mktemp); trap "rm -f $f" EXIT; '
        'head -c 7 > "$f"; sudo -S -p "" bash "$f"'
    )


@pytest.mark.asyncio
async def test_sudo_rejects_control_char_password_before_connect() -> None:
    """AC: a control-char sudo_password is rejected BEFORE the connection opens."""
    from meho_backplane.connectors.linux._sudo import run_remote_bash_with_sudo

    connector = MagicMock()
    connector._connect = AsyncMock()
    for bad in (f"{_SUDO_CANARY}\n", f"{_SUDO_CANARY}\r", f"{_SUDO_CANARY}\x00"):
        with pytest.raises(ValueError, match="single line"):
            await run_remote_bash_with_sudo(connector, _TARGET, "echo hi", sudo_password=bad)
    connector._connect.assert_not_awaited()


# ---------------------------------------------------------------------------
# Command construction + injection safety (pure builders)
# ---------------------------------------------------------------------------


def test_file_write_script_backup_write_validate_rollback_shape() -> None:
    script = build_file_write_script(
        "/etc/app/conf.yaml", _CONTENT_CANARY, backup=True, validate_command="nginx -t"
    )
    # The content + validate command are base64-carried, never spliced raw.
    assert _CONTENT_CANARY not in script
    assert "nginx -t" not in script
    decoded = _decode_b64_tokens(script)
    assert _CONTENT_CANARY in decoded
    assert "nginx -t" in decoded
    # Backup, atomic write, validate, and a rollback branch are all present.
    assert ".meho.bak" in script
    assert 'mktemp "$dir/.meho-write.XXXXXX"' in script
    assert "===MEHO_WRITE_VALIDATE_FAILED===" in script
    assert 'mv -f "$backup" "$path"' in script  # rollback restores the backup
    # The path is shlex.quoted into the assignment.
    assert f"path={shlex.quote('/etc/app/conf.yaml')}" in script


def test_file_write_script_no_validate_no_rollback_branch() -> None:
    script = build_file_write_script("/etc/app/conf.yaml", "x", backup=False, validate_command=None)
    assert "===MEHO_WRITE_VALIDATE_FAILED===" not in script
    assert "do_backup=0" in script
    assert "===MEHO_WRITE_OK===" in script


def test_file_write_script_quotes_a_metachar_path() -> None:
    # A ';' is a valid filename char (confinement rejects only control bytes);
    # the builder must shlex.quote it so it cannot break out of the assignment.
    script = build_file_write_script("/etc/a;rm -rf b", "x", backup=True, validate_command=None)
    assert "path='/etc/a;rm -rf b'" in script
    assert "\npath=/etc/a;rm" not in script


def test_service_control_script_shape() -> None:
    script = build_service_control_script("restart", "nginx.service", daemon_reload=True)
    assert f"systemctl daemon-reload; systemctl restart {shlex.quote('nginx.service')}" in script
    no_reload = build_service_control_script("start", "sshd", daemon_reload=False)
    assert "daemon-reload" not in no_reload
    assert f"systemctl start {shlex.quote('sshd')}" in no_reload


def test_bound_service_action_rejects_unknown() -> None:
    assert bound_service_action("restart") == "restart"
    with pytest.raises(LinuxWriteSafetyError):
        bound_service_action("mask")
    with pytest.raises(LinuxWriteSafetyError):
        bound_service_action("restart; rm -rf /")


def test_script_run_wrapper_carries_values_base64_never_raw() -> None:
    wrapper = build_script_run_wrapper(
        "/usr/bin/python3",
        _SCRIPT_CANARY,
        arguments=_ARG_CANARY,
        working_directory="/opt/app",
        env={"MEHO_ENV_NAME": _ENV_VALUE_CANARY},
    )
    assert _SCRIPT_CANARY not in wrapper
    assert _ENV_VALUE_CANARY not in wrapper
    assert _ARG_CANARY not in wrapper
    decoded = _decode_b64_tokens(wrapper)
    assert _SCRIPT_CANARY in decoded
    assert _ENV_VALUE_CANARY in decoded
    assert _ARG_CANARY in decoded
    assert "export MEHO_ENV_NAME=" in wrapper
    assert f"cd {shlex.quote('/opt/app')}" in wrapper
    assert '/usr/bin/python3 "$tmp" $args' in wrapper


def test_sysctl_write_script_shape() -> None:
    script = build_sysctl_write_script(
        "net.ipv4.ip_forward", "1", "/etc/sysctl.d/60-meho-net.ipv4.ip_forward.conf"
    )
    assert f"sysctl -w {shlex.quote('net.ipv4.ip_forward=1')}" in script
    assert f"printf '%s = %s\\n' {shlex.quote('net.ipv4.ip_forward')} {shlex.quote('1')}" in script
    assert "/etc/sysctl.d/60-meho-net.ipv4.ip_forward.conf" in script


def test_firewall_load_script_validates_before_apply() -> None:
    ruleset = "table inet filter { chain input { type filter hook input priority 0; } }"
    nft = build_firewall_load_script(ruleset, "nft")
    assert ruleset not in nft  # base64-carried
    assert ruleset in _decode_b64_tokens(nft)
    # Validate (`nft -c -f`) appears strictly before apply (`nft -f`).
    assert nft.index('nft -c -f "$tmp"') < nft.index('nft -f "$tmp"')
    assert "===MEHO_FW_VALIDATE_FAILED===" in nft

    ipt = build_firewall_load_script("*filter\n:INPUT DROP\nCOMMIT\n", "iptables")
    assert ipt.index('iptables-restore --test < "$tmp"') < ipt.index('iptables-restore < "$tmp"')


def test_firewall_backend_bounds() -> None:
    assert bound_firewall_backend(None) == "nft"
    assert bound_firewall_backend("iptables") == "iptables"
    with pytest.raises(LinuxWriteSafetyError):
        bound_firewall_backend("ufw")


def test_confine_write_path_allows_config_roots_rejects_others() -> None:
    assert confine_write_path("/etc/app/conf.yaml") == "/etc/app/conf.yaml"
    assert confine_write_path("/usr/local/etc/x") == "/usr/local/etc/x"
    # Read-only roots are NOT write roots.
    for bad in ("/proc/sys/net/ipv4/ip_forward", "/var/log/syslog", "/boot/x", "/x"):
        with pytest.raises(PathConfinementError):
            confine_write_path(bad)
    # Traversal + relative are rejected.
    with pytest.raises(PathConfinementError):
        confine_write_path("/etc/../root/.ssh/authorized_keys")
    with pytest.raises(PathConfinementError):
        confine_write_path("relative/path")


# ---------------------------------------------------------------------------
# Handler-layer (mocked sudo primitive / _run_command)
# ---------------------------------------------------------------------------


def _connector_with_secret() -> Any:
    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(  # type: ignore[method-assign]
        return_value={"username": "root", "sudo_password": _SUDO_CANARY}
    )
    return connector


@pytest.mark.asyncio
async def test_file_write_handler_success() -> None:
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="===MEHO_WRITE_OK===\n", exit_status=0)),
    ):
        result = await linux_file_write(
            connector,
            _TARGET,
            {"path": "/etc/app/conf.yaml", "content": _CONTENT_CANARY, "validate_command": "true"},
        )
    assert result["written"] is True
    assert result["validated"] is True
    assert result["rolled_back"] is False
    assert result["backup_path"] == "/etc/app/conf.yaml.meho.bak"
    assert _CONTENT_CANARY not in repr(result)


@pytest.mark.asyncio
async def test_file_write_handler_validate_failure_rolls_back() -> None:
    """AC: a validate failure leaves the original in place (rolled back)."""
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="===MEHO_WRITE_VALIDATE_FAILED===\n", exit_status=3)),
    ):
        result = await linux_file_write(
            connector,
            _TARGET,
            {"path": "/etc/app/conf.yaml", "content": "bad", "validate_command": "false"},
        )
    assert result["written"] is False
    assert result["validated"] is False
    assert result["rolled_back"] is True


@pytest.mark.asyncio
async def test_file_write_rejects_path_outside_write_roots_no_ssh() -> None:
    connector = _connector_with_secret()
    run_mock = AsyncMock()
    with (
        patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", run_mock),
        pytest.raises(PathConfinementError),
    ):
        await linux_file_write(connector, _TARGET, {"path": "/proc/sys/kernel/x", "content": "1"})
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_write_requires_sudo_credential() -> None:
    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(return_value={"username": "root"})  # type: ignore[method-assign]
    with (
        patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", AsyncMock()),
        pytest.raises(LinuxWriteError),
    ):
        await linux_file_write(connector, _TARGET, {"path": "/etc/x", "content": "y"})


@pytest.mark.asyncio
async def test_service_control_handler_runs_and_reports_ok() -> None:
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(exit_status=0)),
    ):
        result = await linux_service_control(
            connector, _TARGET, {"action": "restart", "unit": "nginx"}
        )
    assert result == {
        "unit": "nginx",
        "action": "restart",
        "daemon_reload": False,
        "ok": True,
    }


@pytest.mark.asyncio
async def test_script_run_handler_returns_stdout_lines_stderr_exit() -> None:
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="line-1\nline-2\n", stderr="warn", exit_status=0)),
    ):
        result = await linux_script_run(
            connector,
            _TARGET,
            {"script": _SCRIPT_CANARY, "use_sudo": True, "arguments": _ARG_CANARY},
        )
    assert result["stdout"] == ["line-1", "line-2"]
    assert result["total"] == 2
    assert result["stderr"] == "warn"
    assert result["exit_code"] == 0
    assert result["used_sudo"] is True
    assert _SCRIPT_CANARY not in repr(result)
    assert _ARG_CANARY not in repr(result)


@pytest.mark.asyncio
async def test_script_run_without_sudo_uses_run_command_not_sudo() -> None:
    connector = LinuxSshConnector()
    run_command = AsyncMock(return_value=_proc(stdout="ok\n", exit_status=0))
    connector._run_command = run_command  # type: ignore[method-assign]
    sudo_mock = AsyncMock()
    with patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", sudo_mock):
        result = await linux_script_run(
            connector, _TARGET, {"script": "echo ok", "use_sudo": False}
        )
    assert result["used_sudo"] is False
    assert result["stdout"] == ["ok"]
    sudo_mock.assert_not_awaited()
    run_command.assert_awaited_once()
    # The wrapper is run under bash, not spliced into the login shell verbatim.
    assert run_command.await_args.args[1].startswith("bash -c ")


def test_script_run_large_stdout_is_a_spillable_collection() -> None:
    """AC: large stdout spills -- it is the single JSONFlux-detected list field."""
    payload = {
        "stdout": [f"line-{i}" for i in range(60)],
        "total": 60,
        "stderr": "",
        "exit_code": 0,
    }
    key, rows = _detect_collection(payload)
    assert key == "stdout"
    assert rows is not None and len(rows) == 60
    reducer = JsonFluxReducer()
    assert reducer._over_threshold(rows, payload) is True


@pytest.mark.asyncio
async def test_sysctl_write_handler_success() -> None:
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="===MEHO_SYSCTL_OK===\n", exit_status=0)),
    ):
        result = await linux_sysctl_write(
            connector, _TARGET, {"key": "net.ipv4.ip_forward", "value": "1"}
        )
    assert result["applied"] is True
    assert result["key"] == "net.ipv4.ip_forward"
    assert result["value"] == "1"
    assert result["dropin_path"] == "/etc/sysctl.d/60-meho-net.ipv4.ip_forward.conf"


@pytest.mark.asyncio
async def test_sysctl_write_rejects_bad_value_no_ssh() -> None:
    connector = _connector_with_secret()
    run_mock = AsyncMock()
    with (
        patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", run_mock),
        pytest.raises(LinuxWriteSafetyError),
    ):
        await linux_sysctl_write(
            connector, _TARGET, {"key": "net.ipv4.ip_forward", "value": "1; rm -rf /"}
        )
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_firewall_load_handler_success() -> None:
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stdout="===MEHO_FW_OK===\n", exit_status=0)),
    ):
        result = await linux_firewall_load(connector, _TARGET, {"ruleset": "table inet filter {}"})
    assert result == {"applied": True, "backend": "nft", "validated": True}


@pytest.mark.asyncio
async def test_firewall_load_validate_failure_leaves_firewall_unchanged() -> None:
    """AC: a ruleset that fails validation never touches the live firewall."""
    connector = _connector_with_secret()
    with patch(
        "meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo",
        AsyncMock(return_value=_proc(stderr="===MEHO_FW_VALIDATE_FAILED===\n", exit_status=3)),
    ):
        result = await linux_firewall_load(connector, _TARGET, {"ruleset": "garbage {{{"})
    assert result["applied"] is False
    assert result["validated"] is False


# ---------------------------------------------------------------------------
# Broadcast clamp membership + classification
# ---------------------------------------------------------------------------


def test_credential_bearing_writes_are_pinned_credential_write() -> None:
    for op_id in ("linux.file.write", "linux.script.run"):
        assert op_id in _CREDENTIAL_WRITE_OPS, op_id
        assert classify_op(op_id) == "credential_write", op_id


def test_non_secret_writes_are_not_credential_class() -> None:
    for op_id in ("linux.sysctl.write", "linux.firewall.load", "linux.service.control"):
        assert op_id not in _CREDENTIAL_WRITE_OPS, op_id
        assert classify_op(op_id) != "credential_write", op_id


# ---------------------------------------------------------------------------
# Park-time bespoke preview redaction (builders registered + never values)
# ---------------------------------------------------------------------------


def test_bespoke_builders_registered_only_for_credential_class_writes() -> None:
    assert "linux.file.write" in _PREVIEW_BUILDERS
    assert "linux.script.run" in _PREVIEW_BUILDERS
    # The non-secret writes use the generic params-echo default, no bespoke one.
    assert "linux.sysctl.write" not in _PREVIEW_BUILDERS
    assert "linux.firewall.load" not in _PREVIEW_BUILDERS


@pytest.mark.asyncio
async def test_file_write_preview_echoes_shape_never_content() -> None:
    ctx = types.SimpleNamespace(
        params={
            "path": "/etc/app/conf.yaml",
            "content": _CONTENT_CANARY,
            "validate_command": "nginx -t",
            "backup": True,
        },
        target=types.SimpleNamespace(name="linux-1"),
    )
    preview = await _linux_file_write_preview(ctx)  # type: ignore[arg-type]
    assert preview == {
        "target": "linux-1",
        "path": "/etc/app/conf.yaml",
        "content_bytes": len(_CONTENT_CANARY.encode("utf-8")),
        "backup_path": "/etc/app/conf.yaml.meho.bak",
        "validate_command": "nginx -t",
    }
    assert _CONTENT_CANARY not in repr(preview)


@pytest.mark.asyncio
async def test_script_run_preview_echoes_shape_never_values() -> None:
    ctx = types.SimpleNamespace(
        params={
            "script": _SCRIPT_CANARY,
            "interpreter": "/usr/bin/python3",
            "arguments": _ARG_CANARY,
            "env": {"MEHO_ENV_NAME": _ENV_VALUE_CANARY},
            "working_directory": "/opt/app",
            "timeout_seconds": 30,
            "use_sudo": True,
        },
        target=types.SimpleNamespace(name="linux-1"),
    )
    preview = await _linux_script_run_preview(ctx)  # type: ignore[arg-type]
    assert preview == {
        "target": "linux-1",
        "interpreter": "/usr/bin/python3",
        "script_bytes": len(_SCRIPT_CANARY.encode("utf-8")),
        "use_sudo": True,
        "argument_bytes": len(_ARG_CANARY.encode("utf-8")),
        "env_var_names": ["MEHO_ENV_NAME"],
        "working_directory": "/opt/app",
        "timeout_seconds": 30,
    }
    for leaked in (_SCRIPT_CANARY, _ARG_CANARY, _ENV_VALUE_CANARY):
        assert leaked not in repr(preview)


# ---------------------------------------------------------------------------
# preview_operation + dispatcher gate (DB-backed)
# ---------------------------------------------------------------------------


_CONNECTOR_ID = "linux-ssh-1.x"
_TARGET_NAME = "linux-write-e2e"
_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003361")
_OPERATOR = Operator(
    sub="linux-write-e2e-test",
    name="Linux Write E2E Operator",
    email=None,
    raw_jwt="<linux-write-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)


@pytest.fixture
async def linux_write_registered() -> AsyncIterator[None]:
    """Register the linux typed ops + seed a linux target against the per-test DB."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.operations.dispatcher import set_default_reducer
    from meho_backplane.operations.reducer import PassThroughReducer

    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    await LinuxSshConnector.register_operations()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_OPERATOR_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="linux",
                host="linux-host.test.invalid",
                port=22,
                fqdn=None,
                secret_ref="kv/dev/linux/write-e2e",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "1.x"},
                notes="seeded by test_connectors_linux_write",
            )
        )
        await session.commit()
    yield
    reset_dispatcher_caches()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("linux.file.write", {"path": "/etc/app/conf.yaml", "content": _CONTENT_CANARY}),
        ("linux.script.run", {"script": _SCRIPT_CANARY, "arguments": _ARG_CANARY}),
    ],
)
async def test_preview_credential_class_writes_are_unavailable_no_secret(
    linux_write_registered: None, op_id: str, params: dict[str, Any]
) -> None:
    """AC: preview_operation returns preview_unavailable for the credential writes."""
    envelope = await preview_operation(
        _OPERATOR,
        {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": _TARGET_NAME, "params": params},
    )
    assert envelope["status"] == "unavailable"
    assert envelope["extras"]["reason"] == "not_ingested"
    assert "redacted_body" not in envelope
    # No secret rides in the envelope.
    for leaked in (_CONTENT_CANARY, _SCRIPT_CANARY, _ARG_CANARY):
        assert leaked not in str(envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("linux.sysctl.write", {"key": "net.ipv4.ip_forward", "value": "1"}),
        ("linux.firewall.load", {"ruleset": "table inet filter {}"}),
    ],
)
async def test_preview_non_secret_writes_are_previewable(
    linux_write_registered: None, op_id: str, params: dict[str, Any]
) -> None:
    """AC: the two non-secret writes preview via the generic params-echo default."""
    envelope = await preview_operation(
        _OPERATOR,
        {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": _TARGET_NAME, "params": params},
    )
    assert envelope["status"] == "ok", envelope
    # The generic params-echo default surfaced the (non-secret) params.
    assert envelope.get("proposed_effect") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("linux.file.write", {"path": "/etc/app/conf.yaml", "content": "x"}),
        ("linux.script.run", {"script": "echo hi"}),
        ("linux.sysctl.write", {"key": "net.ipv4.ip_forward", "value": "1"}),
        ("linux.firewall.load", {"ruleset": "table inet filter {}"}),
    ],
)
async def test_dangerous_writes_park_for_approval(
    linux_write_registered: None, op_id: str, params: dict[str, Any]
) -> None:
    """AC: every dangerous write floors at the approval queue (parks, not runs)."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=False,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval", dumped
    assert dumped.get("extras", {}).get("approval_request_id")


@pytest.mark.asyncio
async def test_service_control_caution_does_not_park(linux_write_registered: None) -> None:
    """AC: service.control is caution -- an un-approved dispatch runs, does not park."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)

    handler = AsyncMock(
        return_value={"unit": "nginx", "action": "restart", "daemon_reload": False, "ok": True}
    )
    with patch("meho_backplane.connectors.linux.ops_write.linux_service_control", handler):
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id="linux.service.control",
            target=target,
            params={"action": "restart", "unit": "nginx"},
            _approved=False,
        )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "ok", dumped
    assert dumped["status"] != "awaiting_approval"
    handler.assert_awaited_once()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.script.run`` keeps every value off the host's command line.

The wrapper (the script, the arguments and every ``env`` and ``secret_env``
value) goes to the host on stdin, with and without sudo. These tests pin:

* without sudo, the remote command is always ``bash -s`` and holds no value,
  also not as base64;
* with sudo, the remote command holds no value either;
* the script still sees each value: the wrapper runs under a local
  ``bash -s`` and the script prints the values it got.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import linux_script_run
from meho_backplane.settings import get_settings

# Synthetic values. Shaped to stay clear of the suite's secret-pattern list.
_SCRIPT_VALUE = "MEHO_STDIN_SCRIPT_VALUE_zzq"
_ARG_VALUE = "MEHO_STDIN_ARG_VALUE_zzq"
_ENV_VALUE = "MEHO_STDIN_ENV_VALUE_zzq"
_SECRET_ENV_VALUE = "MEHO_STDIN_SECRETENV_VALUE_zzq"  # gitleaks:allow -- synthetic
_SUDO_VALUE = "meho-stdin-sudo-value-zzq"  # gitleaks:allow -- synthetic
_SECRET_ENV_PATH = "tenants/00000000-0000-0000-0000-000000003361/app/db"
_SECRET_ENV_FIELD = "cred"

_VALUES = (_SCRIPT_VALUE, _ARG_VALUE, _ENV_VALUE, _SECRET_ENV_VALUE)


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="linux-stdin-test",
    host="linux-host.test.invalid",
    port=22,
    secret_ref="meho/testing/linux/stdin-test",
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.exit_status = exit_status
    return proc


def _connector() -> LinuxSshConnector:
    """A connector whose Vault reads answer from fixed synthetic data."""
    connector = LinuxSshConnector()

    def _resolve(target: Any, operator: Any = None) -> dict[str, Any]:
        if getattr(target, "secret_ref", None) == _SECRET_ENV_PATH:
            return {_SECRET_ENV_FIELD: _SECRET_ENV_VALUE}
        return {"username": "root", "sudo_password": _SUDO_VALUE}

    connector._resolve_secret = AsyncMock(side_effect=_resolve)  # type: ignore[method-assign]
    return connector


def _params(*, use_sudo: bool, script: str = f"echo {_SCRIPT_VALUE}") -> dict[str, Any]:
    return {
        "script": script,
        "arguments": _ARG_VALUE,
        "env": {"APP_PLAIN": _ENV_VALUE},
        "secret_env": {"APP_CRED": f"{_SECRET_ENV_PATH}#{_SECRET_ENV_FIELD}"},
        "use_sudo": use_sudo,
    }


def _assert_no_value(command: str) -> None:
    for value in _VALUES:
        assert value not in command
        assert base64.b64encode(value.encode()).decode() not in command
    assert _SUDO_VALUE not in command


@pytest.mark.asyncio
async def test_without_sudo_the_command_line_is_fixed_and_holds_no_value() -> None:
    connector = _connector()
    run_command = AsyncMock(return_value=_proc(stdout="ok\n"))
    connector._run_command = run_command  # type: ignore[method-assign]

    await linux_script_run(connector, _TARGET, _params(use_sudo=False))

    run_command.assert_awaited_once()
    cmd = run_command.await_args.args[1]
    assert cmd == "bash -s"
    _assert_no_value(cmd)
    # The values travel on stdin, inside the wrapper.
    stdin = run_command.await_args.kwargs["stdin"]
    assert "export APP_PLAIN=" in stdin
    assert "export APP_CRED=" in stdin


@pytest.mark.asyncio
async def test_two_calls_without_sudo_send_the_same_command_line() -> None:
    connector = _connector()
    run_command = AsyncMock(return_value=_proc())
    connector._run_command = run_command  # type: ignore[method-assign]

    await linux_script_run(connector, _TARGET, _params(use_sudo=False))
    await linux_script_run(connector, _TARGET, {"script": "true", "use_sudo": False})

    first, second = (call.args[1] for call in run_command.await_args_list)
    assert first == second == "bash -s"


@pytest.mark.asyncio
async def test_with_sudo_the_command_line_holds_no_value() -> None:
    connector = _connector()
    connector._connect = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    run_capped = AsyncMock(return_value=_proc(stdout="ok\n"))

    with patch("meho_backplane.connectors.linux._sudo.run_capped", run_capped):
        await linux_script_run(connector, _TARGET, _params(use_sudo=True))

    run_capped.assert_awaited_once()
    cmd = run_capped.await_args.args[1]
    _assert_no_value(cmd)
    assert "export APP_CRED=" in run_capped.await_args.kwargs["stdin"]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs a local bash")
async def test_the_script_still_sees_every_value() -> None:
    """Run the wrapper the way the host does: ``bash -s`` with the wrapper on stdin."""
    connector = _connector()
    sent: dict[str, str] = {}

    async def _run(target: Any, cmd: str, **kwargs: Any) -> Any:
        done = subprocess.run(
            cmd.split(),
            input=kwargs["stdin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        sent["stderr"] = done.stderr
        return _proc(stdout=done.stdout, exit_status=done.returncode)

    connector._run_command = _run  # type: ignore[method-assign]
    script = 'printf "%s|%s|%s\\n" "$APP_PLAIN" "$APP_CRED" "$*"'

    result = await linux_script_run(
        connector, _TARGET, _params(use_sudo=False, script=script) | {"interpreter": "/bin/bash"}
    )

    assert result["exit_code"] == 0, sent.get("stderr")
    assert result["stdout"] == [f"{_ENV_VALUE}|{_SECRET_ENV_VALUE}|{_ARG_VALUE}"]

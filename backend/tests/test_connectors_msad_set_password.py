# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``msad.user.set-password`` and the stdin seam it uses.

The op reads the password from the secret store at run time and sends it to
PowerShell on standard input. These tests check that the password never
reaches the script, the remote command line, the result or an error, and that
a bad reference stops the op before anything runs on the server.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.auth.vault import VaultClientError
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.msad import MSAD_OPS, MsadConnector
from meho_backplane.connectors.msad.ops_password import (
    SET_PASSWORD_OP_ID,
    msad_user_set_password,
    parse_password_ref,
    set_password_script,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

_PASSWORD = "Pässwört'; Remove-ADUser x #\u2019"


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
    name="msad-test",
    host="dc.test.invalid",
    port=22,
    secret_ref="meho/testing/msad/msad-test",
)


def _proc(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


_OK = '{"SamAccountName":"jdoe","Enabled":true,"PwdLastSet":133000000000000000}'


def _script(run_mock: AsyncMock) -> str:
    cmd: str = run_mock.await_args.args[1]
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")


async def _call(
    params: dict[str, Any], *, secret: dict[str, Any] | None = None, stdout: str = _OK
) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
    connector = MsadConnector()
    resolve = AsyncMock(return_value=secret if secret is not None else {"password": _PASSWORD})
    run_mock = AsyncMock(return_value=_proc(stdout=stdout))
    with (
        patch.object(connector, "_resolve_secret", resolve),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await msad_user_set_password(connector, _TARGET, params)
    return result, resolve, run_mock


# ---------------------------------------------------------------------------
# The op
# ---------------------------------------------------------------------------


async def test_password_goes_on_stdin_never_in_the_command() -> None:
    result, resolve, run_mock = await _call(
        {"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"}
    )
    stdin = run_mock.await_args.kwargs["stdin"]
    assert base64.b64decode(stdin.strip()).decode("utf-8") == _PASSWORD
    cmd: str = run_mock.await_args.args[1]
    assert _PASSWORD not in cmd
    assert _PASSWORD not in _script(run_mock)
    assert stdin.strip() not in _script(run_mock)
    assert _PASSWORD not in repr(result)
    # The vault read used the reference path, under the target's name.
    stub = resolve.await_args.args[0]
    assert stub.secret_ref == "lab/ad/jdoe"
    assert stub.name == "msad-test"


async def test_result_reports_account_state() -> None:
    result, _, _ = await _call({"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"})
    assert result == {
        "identity": "jdoe",
        "sam_account_name": "jdoe",
        "enabled": True,
        "password_last_set": "2022-06-18T04:26:40Z",
        "op_class": "write",
    }


async def test_script_resets_enables_and_reads_back() -> None:
    _, _, run_mock = await _call({"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"})
    script = _script(run_mock)
    assert "$b = @($input)[0];" in script
    assert "FromBase64String($b)" in script
    assert "Set-ADAccountPassword -Identity 'jdoe' -Reset -NewPassword $p;" in script
    assert "Enable-ADAccount -Identity 'jdoe';" in script
    assert "Get-ADUser -Identity 'jdoe'" in script


async def test_enable_false_skips_enable() -> None:
    _, _, run_mock = await _call(
        {"identity": "jdoe", "password_ref": "lab/ad/jdoe#password", "enable": False}
    )
    assert "Enable-ADAccount" not in _script(run_mock)


def test_identity_is_quoted() -> None:
    script = set_password_script("o'brien\u2019x", enable=True)
    assert "-Identity 'o''brien\u2019\u2019x'" in script


@pytest.mark.parametrize(
    "ref",
    [None, "", "lab/ad/jdoe", "a#b#c", "#password", "lab/ad/jdoe#", "lab\n#x", 42],
)
async def test_bad_reference_runs_nothing(ref: Any) -> None:
    connector = MsadConnector()
    resolve = AsyncMock()
    run_mock = AsyncMock()
    with (
        patch.object(connector, "_resolve_secret", resolve),
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ValueError, match="password_ref"),
    ):
        await msad_user_set_password(connector, _TARGET, {"identity": "jdoe", "password_ref": ref})
    resolve.assert_not_awaited()
    run_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ({"other": "x"}, "has no field"),
        ({"password": ""}, "is empty"),
        ({"password": "   \n"}, "is empty"),
        ({"password": None}, "is not a string"),
        ({"password": {"value": "x"}}, "is not a string"),
        ({"password": ["x"]}, "is not a string"),
        ({"password": 12345}, "is not a string"),
        ({"password": True}, "is not a string"),
    ],
)
async def test_missing_empty_or_non_string_field_runs_nothing(
    secret: dict[str, Any], message: str
) -> None:
    connector = MsadConnector()
    run_mock = AsyncMock()
    with (
        patch.object(connector, "_resolve_secret", AsyncMock(return_value=secret)),
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ValueError, match=f"password_ref.*{message}"),
    ):
        await msad_user_set_password(
            connector, _TARGET, {"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"}
        )
    run_mock.assert_not_awaited()


async def test_vault_error_names_the_reference_and_runs_nothing() -> None:
    connector = MsadConnector()
    run_mock = AsyncMock()
    with (
        patch.object(
            connector, "_resolve_secret", AsyncMock(side_effect=VaultClientError("denied"))
        ),
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ValueError, match="lab/ad/jdoe#password"),
    ):
        await msad_user_set_password(
            connector, _TARGET, {"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"}
        )
    run_mock.assert_not_awaited()


@pytest.mark.parametrize(
    "params", [{"password_ref": "a#b"}, {"identity": " ", "password_ref": "a#b"}]
)
async def test_bad_identity_runs_nothing(params: dict[str, Any]) -> None:
    connector = MsadConnector()
    resolve = AsyncMock()
    with (
        patch.object(connector, "_resolve_secret", resolve),
        pytest.raises(ValueError, match="identity"),
    ):
        await msad_user_set_password(connector, _TARGET, params)
    resolve.assert_not_awaited()


def test_parse_password_ref_strips_spaces() -> None:
    assert parse_password_ref(" lab/ad/jdoe # password ") == ("lab/ad/jdoe", "password")


# ---------------------------------------------------------------------------
# Metadata, broadcast pin and preview
# ---------------------------------------------------------------------------


def test_op_waits_for_approval_and_is_a_credential_write() -> None:
    op = {o.op_id: o for o in MSAD_OPS}[SET_PASSWORD_OP_ID]
    assert op.safety_level == "caution"
    assert op.requires_approval is True
    assert "credential" in op.tags
    assert classify_op(SET_PASSWORD_OP_ID) == "credential_write"


async def test_preview_shows_reference_not_value() -> None:
    builder = _PREVIEW_BUILDERS[SET_PASSWORD_OP_ID]
    ctx = MagicMock(spec=PreviewContext)
    ctx.params = {"identity": "jdoe", "password_ref": "lab/ad/jdoe#password"}
    ctx.target = _TARGET
    assert await builder(ctx) == {
        "target": "msad-test",
        "identity": "jdoe",
        "password_ref": "lab/ad/jdoe#password",
        "enable": True,
    }
    ctx.params = {"identity": "jdoe", "password_ref": "no-field"}
    assert await builder(ctx) is None


# ---------------------------------------------------------------------------
# The stdin seam
# ---------------------------------------------------------------------------


async def test_pwsh_run_without_stdin_keeps_the_old_call() -> None:
    connector = MsadConnector()
    run_mock = AsyncMock(return_value=_proc(stdout="{}"))
    with patch.object(connector, "_run_command", run_mock):
        await pwsh_run(connector, _TARGET, "x")
    assert "stdin" not in run_mock.await_args.kwargs


async def test_pwsh_error_does_not_carry_stdin() -> None:
    connector = MsadConnector()
    run_mock = AsyncMock(return_value=_proc(stderr="bad", exit_status=1))
    with patch.object(connector, "_run_command", run_mock), pytest.raises(Exception) as info:
        await pwsh_run(connector, _TARGET, "x", stdin="c2VjcmV0\n")
    assert "c2VjcmV0" not in str(info.value)
    assert "c2VjcmV0" not in getattr(info.value, "stderr", "")


class _Ssh(SshConnector):
    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


async def test_run_command_passes_stdin_as_input() -> None:
    connector = _Ssh()
    conn = MagicMock()
    conn.run = AsyncMock(return_value=_proc(stdout="ok"))
    with patch.object(connector, "_connect", AsyncMock(return_value=conn)):
        await connector._run_command(_TARGET, "cmd", stdin="line\n")  # type: ignore[arg-type]
        conn.run.assert_awaited_with("cmd", input="line\n", check=False)
        await connector._run_command(_TARGET, "cmd")  # type: ignore[arg-type]
        conn.run.assert_awaited_with("cmd", check=False)

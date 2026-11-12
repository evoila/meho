# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the linux-ssh host-readiness reads.

Covers ``linux.tool.which``, ``linux.identity.show`` and ``linux.http.head``:
command construction, injection safety, the failure cases (a failed probe is
an error, never "not found"), and that no secret-shaped value is returned.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.linux import LINUX_OPS, LinuxSshConnector
from meho_backplane.connectors.linux.ops import LINUX_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.linux.ops_diagnose import (
    DIAGNOSE_OPS,
    HTTP_HEAD_TIMEOUT_S,
    LinuxDiagnoseError,
    build_http_head_command,
    build_identity_command,
    build_tool_which_command,
    parse_http_head_output,
    parse_identity_output,
    parse_tool_which_output,
    validate_tool_names,
    validate_url,
)
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets


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


_SECRET_PATH = "meho/testing/linux/diagnose-test"
_TARGET = _StubTarget("linux-diagnose-test", "linux-host.test.invalid", 22, _SECRET_PATH)


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets({_SECRET_PATH: {"username": "root", "password": "made-up-pw-1"}}):
        yield


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


async def _run(op: str, params: dict[str, Any], proc: Any) -> tuple[Any, AsyncMock]:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = proc
        result = await getattr(connector, op)(_TARGET, params)
    return result, mock_cmd


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_NEW = {
    "linux.tool.which": "system",
    "linux.identity.show": "system",
    "linux.http.head": "network",
}


def test_new_ops_are_safe_read_only_and_grouped() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    assert {op.op_id for op in DIAGNOSE_OPS} == set(_NEW)
    for op_id, group in _NEW.items():
        op = by_id[op_id]
        # http.head takes an address the host connects to: MEHO's address floor
        # makes it caution + approval. The other two stay safe.
        assert (op.safety_level, op.requires_approval) == (
            ("caution", True) if op_id == "linux.http.head" else ("safe", False)
        )
        assert op.group_key == group
        assert op_id in LINUX_WHEN_TO_USE_BY_GROUP[group]
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions and op.response_schema


def test_new_ops_declare_no_secret_parameter() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    for op_id in _NEW:
        for name in by_id[op_id].parameter_schema["properties"]:
            assert "password" not in name and "secret" not in name


# ---------------------------------------------------------------------------
# tool.which
# ---------------------------------------------------------------------------


def test_tool_which_command_quotes_every_name() -> None:
    cmd = build_tool_which_command(["curl", "nfs-utils", "g++"])
    assert cmd.startswith(f"for n in curl nfs-utils {shlex.quote('g++')}; do ")
    assert "command -v" in cmd


@pytest.mark.parametrize(
    "bad",
    [
        ["curl; reboot"],
        ["$(id)"],
        ["`id`"],
        ["a b"],
        ["-x"],
        ["/bin/sh"],
        ["a|b"],
        ["a\nb"],
        [""],
        ["a" * 65],
        [5],
        [],
        ["a"] * 2,
        [f"t{i}" for i in range(21)],
        "curl",
        None,
    ],
)
def test_tool_names_refused_before_any_command(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_tool_names(bad)
    with pytest.raises(ValueError):
        build_tool_which_command(bad)


def test_tool_which_parser_reads_found_missing_and_builtin() -> None:
    out = "FOUND\tcurl\t/usr/bin/curl\nMISSING\tnfsstat\nFOUND\techo\techo\n"
    assert parse_tool_which_output(["curl", "nfsstat", "echo"], out) == {
        "tools": [
            {"name": "curl", "found": True, "path": "/usr/bin/curl"},
            {"name": "nfsstat", "found": False, "path": None},
            {"name": "echo", "found": True, "path": None},
        ]
    }


def test_tool_which_parser_raises_when_a_name_has_no_answer() -> None:
    with pytest.raises(LinuxDiagnoseError):
        parse_tool_which_output(["curl", "jq"], "FOUND\tcurl\t/usr/bin/curl\n")


@pytest.mark.asyncio
async def test_tool_which_handler_runs_fixed_command() -> None:
    result, mock_cmd = await _run(
        "tool_which",
        {"names": ["curl"]},
        _proc(stdout="FOUND\tcurl\t/usr/bin/curl\n"),
    )
    assert result["tools"][0]["found"] is True
    assert mock_cmd.await_args_list[0].args[1] == build_tool_which_command(["curl"])


@pytest.mark.asyncio
async def test_tool_which_handler_raises_on_failed_command() -> None:
    with pytest.raises(LinuxDiagnoseError):
        await _run("tool_which", {"names": ["curl"]}, _proc(stdout="", exit_status=255))


@pytest.mark.asyncio
async def test_tool_which_handler_rejects_bad_name_before_ssh() -> None:
    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.tool_which(_TARGET, {"names": ["curl; reboot"]})
    mock_cmd.assert_not_awaited()


# ---------------------------------------------------------------------------
# identity.show
# ---------------------------------------------------------------------------

_ID_OK = "user=ops\nuid=1000\ngid=1000\ngroups=ops sudo docker\nsudo_rc=0\n"


def test_identity_command_is_fixed_and_runs_only_sudo_n_true() -> None:
    cmd = build_identity_command()
    assert "sudo -n true </dev/null" in cmd
    assert cmd.count("sudo -n") == 1


def test_identity_parser_passwordless_sudo() -> None:
    assert parse_identity_output(_ID_OK) == {
        "user": "ops",
        "uid": 1000,
        "gid": 1000,
        "groups": ["ops", "sudo", "docker"],
        "is_root": False,
        "sudo_installed": True,
        "sudo_passwordless": True,
        "sudo_exit_code": 0,
    }


def test_identity_parser_password_needed_and_root_and_no_sudo() -> None:
    needs = parse_identity_output(_ID_OK.replace("sudo_rc=0", "sudo_rc=1"))
    assert needs["sudo_installed"] is True
    assert needs["sudo_passwordless"] is False
    root = parse_identity_output("user=root\nuid=0\ngid=0\ngroups=root\nsudo_rc=127\n")
    assert root["is_root"] is True
    assert root["sudo_installed"] is False
    assert root["sudo_passwordless"] is False


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "user=\nuid=\ngid=\ngroups=\nsudo_rc=0\n",
        "user=ops\nuid=x\ngid=1\ngroups=ops\nsudo_rc=0\n",
        "user=ops\nuid=1\ngid=1\ngroups=ops\n",
    ],
)
def test_identity_parser_raises_on_unreadable_answer(stdout: str) -> None:
    with pytest.raises(LinuxDiagnoseError):
        parse_identity_output(stdout)


@pytest.mark.asyncio
async def test_identity_handler_ignores_params_and_runs_fixed_command() -> None:
    result, mock_cmd = await _run("identity_show", {"x": "ignored"}, _proc(stdout=_ID_OK))
    assert result["user"] == "ops"
    assert mock_cmd.await_args_list[0].args[1] == build_identity_command()


# ---------------------------------------------------------------------------
# http.head
# ---------------------------------------------------------------------------

_HEAD_OK = (
    "HTTP/2 200 \r\ncontent-type: text/html\r\nset-cookie: sid=made-up-cookie\r\n"
    "x-multi: a\r\nx-multi: b\r\n\r\n"
)


@pytest.mark.parametrize(
    "url",
    [
        "https://mirror.example.com/repo/",
        "http://mirror.example.com:8080/a/b.txt?x=1;y=2",
        "https://[2001:db8::1]:443/",
        "http://10.0.0.1",
    ],
)
def test_http_head_command_is_exact_for_valid_urls(url: str) -> None:
    cmd = build_http_head_command(url)
    assert cmd.endswith(f" {shlex.quote(url)}")
    assert "curl -q -sS -I -g --proto '=http,https' --max-time 15 " in cmd
    assert " -L" not in cmd


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_http_head_command_ignores_the_login_curlrc(tmp_path: Path) -> None:
    # A .curlrc in HOME must not change the request. If curl read it, the
    # write-out marker would be printed.
    (tmp_path / ".curlrc").write_text('-w "CURLRC-LOADED"\n-L\n-X POST\n')
    proc = subprocess.run(
        ["sh", "-c", build_http_head_command("http://127.0.0.1:1/")],
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 7
    assert "CURLRC-LOADED" not in proc.stdout


def test_identity_guidance_does_not_say_password_sudo_fails() -> None:
    op = next(op for op in DIAGNOSE_OPS if op.op_id == "linux.identity.show")
    hints = op.llm_instructions
    assert hints is not None
    assert "will fail" not in hints["next_step"]
    assert "stored sudo password" in hints["next_step"]
    assert "only sudo without a password" in hints["next_step"]


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "example.com",
        "https://user:pw@example.com/",
        "https://example.com/a b",
        "https://example.com/;reboot'",
        "https://example.com/$(id)",
        "https://example.com/`id`",
        "https://example.com/a&b=c",
        "https://example.com/a|b",
        "https://example.com/a>b",
        "https://example.com/#frag",
        "https://example.com/\nx",
        "https://-example.com/",
        "https://example.com/" + "a" * 2100,
        "",
        None,
        5,
    ],
)
def test_url_refused_before_any_command(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_url(bad)
    with pytest.raises(ValueError):
        build_http_head_command(bad)


def test_http_head_parser_reachable_drops_cookies_and_joins_repeats() -> None:
    result = parse_http_head_output("https://x.example.com/", _HEAD_OK, "", 0)
    assert result["reachable"] is True
    assert result["status_code"] == 200
    assert result["headers"] == {"content-type": "text/html", "x-multi": "a, b"}
    assert "made-up-cookie" not in repr(result)


def test_http_head_parser_error_status_is_still_reachable() -> None:
    result = parse_http_head_output(
        "https://x.example.com/", "HTTP/1.1 503 Service Unavailable\r\n\r\n", "", 0
    )
    assert result["reachable"] is True
    assert result["status_code"] == 503


def test_http_head_parser_takes_the_last_block() -> None:
    out = "HTTP/1.1 200 Connection established\r\n\r\nHTTP/1.1 404 Not Found\r\nx: y\r\n\r\n"
    assert parse_http_head_output("https://x.example.com/", out, "", 0)["status_code"] == 404


@pytest.mark.parametrize("code", [6, 7, 28, 35, 60])
def test_http_head_parser_network_failure_is_unreachable_with_reason(code: int) -> None:
    result = parse_http_head_output(
        "https://x.example.com/", "", "curl: (6) Could not resolve host: x.example.com\n", code
    )
    assert result["reachable"] is False
    assert result["status_code"] is None
    assert result["curl_exit_code"] == code
    assert "Could not resolve host" in result["error"]


@pytest.mark.parametrize("code", [1, 2, 3, 4, 127])
def test_http_head_parser_probe_failure_raises(code: int) -> None:
    with pytest.raises(LinuxDiagnoseError):
        parse_http_head_output("https://x.example.com/", "", "", code)


@pytest.mark.parametrize("stdout", ["", "garbage\r\n\r\n", "HTTP/1.1 abc\r\n\r\n"])
def test_http_head_parser_raises_when_exit_zero_but_no_status_line(stdout: str) -> None:
    with pytest.raises(LinuxDiagnoseError):
        parse_http_head_output("https://x.example.com/", stdout, "", 0)


def test_http_head_parser_caps_headers_and_values() -> None:
    lines = "".join(f"h{i}: v\r\n" for i in range(200)) + "long: " + "z" * 5000 + "\r\n"
    out = parse_http_head_output(
        "https://x.example.com/", "HTTP/1.1 200 OK\r\n" + lines + "\r\n", "", 0
    )
    assert len(out["headers"]) <= 64
    assert all(len(v) <= 512 for v in out["headers"].values())


@pytest.mark.asyncio
async def test_http_head_handler_runs_fixed_command_with_timeout() -> None:
    result, mock_cmd = await _run(
        "http_head", {"url": "https://mirror.example.com/"}, _proc(stdout=_HEAD_OK)
    )
    assert result["status_code"] == 200
    call = mock_cmd.await_args_list[0]
    assert call.args[1] == build_http_head_command("https://mirror.example.com/")
    assert call.kwargs["timeout"] == HTTP_HEAD_TIMEOUT_S


@pytest.mark.asyncio
async def test_http_head_handler_rejects_bad_url_before_ssh() -> None:
    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.http_head(_TARGET, {"url": "https://example.com/$(id)"})
    mock_cmd.assert_not_awaited()

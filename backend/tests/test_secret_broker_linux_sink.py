# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``linux`` store of ``secret.move``: write a secret to a file on a Linux host.

Three layers:

* the ref and the path checks (no SSH at all);
* the root script, run with the local ``bash`` (owner = the test user, a fake
  ``getent`` for the home directory), to see what it really does on disk;
* the adapter and a full ``secret.move`` with a fake SSH channel, to see that
  the value reaches no place but the SSH standard input.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import stat
import subprocess
import sys
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import meho_backplane.connectors.secret  # noqa: F401 -- registers the linux kind
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.secret_endpoint import (
    LinuxSecretRefError,
    LinuxSecretSinkEndpoint,
    LinuxSecretSinkError,
    build_linux_secret_endpoint,
    build_secret_sink_script,
    parse_linux_secret_ref,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import (
    SECRET_ENDPOINT_REGISTRY,
    SecretMaterial,
    parse_secret_ref,
)
from meho_backplane.connectors.secret.move_preview import build_secret_move_preview
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_SENTINEL = "linux-sink-secret-DO-NOT-LEAK-zq81"  # gitleaks:allow -- synthetic
_SUDO_CANARY = "linux-sink-sudo-canary-qq42"  # gitleaks:allow -- synthetic
_TARGET_NAME = "seat-host"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_OPERATOR = Operator(
    sub="linux-sink-test",
    name=None,
    email=None,
    raw_jwt="<linux-sink-raw-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)
_STUB_TARGET = types.SimpleNamespace(
    name=_TARGET_NAME, host="seat-host.test.invalid", port=22, secret_ref="tenants/x/ssh"
)

_NEEDS_LINUX = pytest.mark.skipif(
    sys.platform != "linux", reason="the sink script uses GNU realpath and install"
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Ref and path checks (no SSH)
# ---------------------------------------------------------------------------


def test_linux_kind_is_registered() -> None:
    assert "linux" in SECRET_ENDPOINT_REGISTRY
    spec = parse_secret_ref("linux:seat-host/etc/app/key#app")
    assert isinstance(SECRET_ENDPOINT_REGISTRY[spec.kind](spec.ref), LinuxSecretSinkEndpoint)


@pytest.mark.parametrize(
    "ref",
    ["seat-host/etc/app/key#app", "seat-host//etc/app/key#app", "seat-host/etc//app/./key#app"],
)
def test_ref_parses_with_one_or_two_slashes(ref: str) -> None:
    assert parse_linux_secret_ref(ref) == ("seat-host", "/etc/app/key", "app")


def test_ref_in_the_owner_home_passes_the_lexical_check() -> None:
    assert parse_linux_secret_ref("seat-host/home/agent/.config/meho/key#agent") == (
        "seat-host",
        "/home/agent/.config/meho/key",
        "agent",
    )


@pytest.mark.parametrize(
    "ref",
    [
        "seat-host/etc/app/key",  # no owner
        "seat-host/etc/app/key#",  # empty owner
        "seat-host/etc/app/key#Bad Owner",  # bad owner
        "seat-host/etc/app/key#a;b",  # shell character in owner
        "seat-host#app",  # no path
        "seat-host/#app",  # empty path
        "/etc/app/key#app",  # no target
        "seat-host/etc/../root/key#app",  # traversal
        "seat-host/home/agent/../../etc/key#agent",  # traversal
        "seat-host/etc/app/#app",  # a directory
        "seat-host/proc/sys/x#app",  # outside every allowed place
        "seat-host/boot/x#app",
        "seat-host/usr/bin/x#app",
        "seat-host/var/log/x#app",
        "seat-host/tmp/x#app",
        "seat-host/etc/a\\nb#app",  # backslash
    ],
)
def test_bad_refs_are_refused_before_any_ssh(ref: str) -> None:
    with pytest.raises(LinuxSecretRefError):
        parse_linux_secret_ref(ref)
    with pytest.raises(LinuxSecretRefError):
        build_linux_secret_endpoint(ref)


@pytest.mark.parametrize("ref", ["seat-host/usr/local/x#app", "seat-host/var/lib/app/x#app"])
def test_write_roots_inside_never_roots_stay_allowed(ref: str) -> None:
    assert parse_linux_secret_ref(ref)[2] == "app"


@pytest.mark.asyncio
async def test_a_refused_path_opens_no_ssh_connection() -> None:
    connect = AsyncMock()
    with (
        patch.object(LinuxSshConnector, "_connect", connect),
        pytest.raises(LinuxSecretRefError),
    ):
        endpoint = LinuxSecretSinkEndpoint("seat-host/etc/../root/key#app")
        await endpoint.write_secret(_OPERATOR, SecretMaterial(_SENTINEL))
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reading_a_linux_ref_fails_without_a_value() -> None:
    endpoint = LinuxSecretSinkEndpoint("seat-host/etc/app/key#app")
    with pytest.raises(NotImplementedError) as excinfo:
        await endpoint.read_secret(_OPERATOR)
    assert "write-only" in str(excinfo.value)
    assert _SENTINEL not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The root script, run locally
# ---------------------------------------------------------------------------


def _run_script(script: str, *, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run *script* with the local bash.

    A fake ``getent`` answers with *home*. A fake ``runuser`` writes its owner
    argument to ``runuser.log`` next to the home, then runs the command (the
    test user already is the owner).
    """
    env = dict(os.environ)
    if home is not None:
        fake_bin = home.parent / f"{home.name}-bin"
        fake_bin.mkdir(exist_ok=True)
        getent = fake_bin / "getent"
        getent.write_text(f"#!/bin/sh\necho 'x:x:1:1::{home}:/bin/sh'\n")
        getent.chmod(0o755)
        runuser = fake_bin / "runuser"
        runuser.write_text(
            f'#!/bin/sh\necho "$2" >> "{fake_bin}/runuser.log"\nshift 3\nexec "$@"\n'
        )
        runuser.chmod(0o755)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@_NEEDS_LINUX
def test_script_writes_the_file_0600_with_a_0700_parent(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    target = home / ".config" / "meho" / "client-secret"
    owner = getpass.getuser()

    proc = _run_script(build_secret_sink_script(str(target), owner, _SENTINEL.encode()), home=home)

    assert proc.returncode == 0, proc.stderr
    assert "===MEHO_SINK_OK===" in proc.stdout
    assert target.read_bytes() == _SENTINEL.encode()
    assert _mode(target) == 0o600
    assert target.stat().st_uid == os.getuid()
    assert _mode(target.parent) == 0o700
    assert _mode(target.parent.parent) == 0o700
    assert [p.name for p in target.parent.iterdir()] == ["client-secret"]
    assert _SENTINEL not in proc.stdout + proc.stderr


@_NEEDS_LINUX
def test_script_run_twice_writes_the_same_value(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    target = home / "key"
    script = build_secret_sink_script(str(target), getpass.getuser(), b"first-value")
    assert _run_script(script, home=home).returncode == 0
    assert _run_script(script, home=home).returncode == 0
    assert target.read_bytes() == b"first-value"
    assert _mode(target) == 0o600


@_NEEDS_LINUX
def test_script_replaces_an_older_file_and_fixes_its_mode(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    target = home / "key"
    target.write_text("old")
    target.chmod(0o644)
    proc = _run_script(build_secret_sink_script(str(target), getpass.getuser(), b"new"), home=home)
    assert proc.returncode == 0, proc.stderr
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600


@_NEEDS_LINUX
def test_script_refuses_a_place_outside_the_home(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "key"
    proc = _run_script(build_secret_sink_script(str(other), getpass.getuser(), b"v"), home=home)
    assert proc.returncode == 6
    assert "===MEHO_SINK_ERR:place===" in proc.stderr
    assert not other.exists()
    assert not other.parent.exists()


@_NEEDS_LINUX
def test_script_refuses_a_link_that_leads_out_of_the_home(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside)
    proc = _run_script(
        build_secret_sink_script(str(home / "link" / "key"), getpass.getuser(), b"v"), home=home
    )
    assert proc.returncode == 6
    assert not (outside / "key").exists()


@_NEEDS_LINUX
def test_script_writes_inside_the_home_as_the_owner(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    owner = getpass.getuser()
    target = home / "a" / "key"
    proc = _run_script(build_secret_sink_script(str(target), owner, b"v"), home=home)
    assert proc.returncode == 0, proc.stderr
    log = home.parent / f"{home.name}-bin" / "runuser.log"
    assert log.read_text().split() == [owner]


@_NEEDS_LINUX
def test_script_has_no_root_step_for_a_home_path() -> None:
    script = build_secret_sink_script("/home/seat/key", "seat", b"v")
    before_roots = script.split('if [ -n "$realhome" ]')[1].split("Write roots")[0]
    assert "runuser -u" in before_roots
    # Root-only steps come after the owner branch has exited.
    owner_branch, _, root_part = script.partition("exit 0;;")
    assert "install -d" not in owner_branch.split('if [ -n "$realhome" ]')[1]
    assert "chown" not in owner_branch.split('if [ -n "$realhome" ]')[1]
    assert "install -d" in root_part


@_NEEDS_LINUX
def test_script_does_not_follow_a_link_in_the_final_name(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (home / "key").symlink_to(victim)
    proc = _run_script(
        build_secret_sink_script(str(home / "key"), getpass.getuser(), b"v"), home=home
    )
    assert victim.read_text() == "keep"
    assert proc.returncode in (0, 6)


@_NEEDS_LINUX
def test_script_refuses_an_unknown_owner(tmp_path: Path) -> None:
    home = tmp_path / "home" / "seat"
    home.mkdir(parents=True)
    proc = _run_script(
        build_secret_sink_script(str(home / "key"), "no-such-user-zq", b"v"), home=home
    )
    assert proc.returncode == 5
    assert "===MEHO_SINK_ERR:owner===" in proc.stderr
    assert not (home / "key").exists()


def test_script_carries_the_value_as_base64_only() -> None:
    script = build_secret_sink_script("/etc/app/key", "app", _SENTINEL.encode())
    assert _SENTINEL not in script
    assert base64.b64encode(_SENTINEL.encode()).decode() in script


# ---------------------------------------------------------------------------
# The adapter with a fake SSH channel
# ---------------------------------------------------------------------------


class _Wire:
    """What the fake SSH channel saw."""

    def __init__(self, stdout: str, stderr: str = "", exit_status: int = 0) -> None:
        self.commands: list[str] = []
        self.stdin: list[str | None] = []
        self._proc = MagicMock(stdout=stdout, stderr=stderr, exit_status=exit_status)

    async def run_capped(self, conn: Any, cmd: str, **kwargs: Any) -> Any:
        self.commands.append(cmd)
        self.stdin.append(kwargs.get("stdin"))
        return self._proc


def _patch_wire(wire: _Wire) -> Any:
    connector = LinuxSshConnector()
    connector._connect = AsyncMock(return_value=object())  # type: ignore[method-assign]
    connector._resolve_secret = AsyncMock(  # type: ignore[method-assign]
        return_value={"username": "ops", "sudo_password": _SUDO_CANARY}
    )
    stack = [
        patch("meho_backplane.connectors.linux._sudo.run_capped", wire.run_capped),
        patch(
            "meho_backplane.connectors.linux.secret_endpoint.get_or_create_connector_instance",
            return_value=connector,
        ),
        patch(
            "meho_backplane.connectors.linux.secret_endpoint.resolve_target",
            AsyncMock(return_value=_STUB_TARGET),
        ),
        patch("meho_backplane.connectors.linux.secret_endpoint.get_sessionmaker", MagicMock()),
    ]
    return stack


@pytest.mark.asyncio
async def test_write_secret_sends_the_value_only_on_stdin(caplog: pytest.LogCaptureFixture) -> None:
    wire = _Wire(stdout="===MEHO_SINK_OK===\n")
    endpoint = LinuxSecretSinkEndpoint(f"{_TARGET_NAME}/home/seat/.config/key#seat")
    with caplog.at_level(logging.DEBUG):
        p1, p2, p3, p4 = _patch_wire(wire)
        with p1, p2, p3, p4:
            await endpoint.write_secret(_OPERATOR, SecretMaterial(_SENTINEL))

    assert len(wire.commands) == 1
    assert _SENTINEL not in wire.commands[0]
    assert base64.b64encode(_SENTINEL.encode()).decode() not in wire.commands[0]
    assert _SUDO_CANARY not in wire.commands[0]
    stdin = wire.stdin[0]
    assert stdin is not None
    assert base64.b64encode(_SENTINEL.encode()).decode() in stdin
    assert "/home/seat/.config/key" in stdin
    assert _SENTINEL not in caplog.text
    assert _SUDO_CANARY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("===MEHO_SINK_ERR:place===\n", "place"),
        ("===MEHO_SINK_ERR:owner===\n", "owner"),
        (f"boom {_SENTINEL}\n", "unknown"),  # a host message is never passed on
        ("", "unknown"),
    ],
)
async def test_a_failed_write_raises_an_error_without_the_value(stderr: str, reason: str) -> None:
    wire = _Wire(stdout="", stderr=stderr, exit_status=6)
    endpoint = LinuxSecretSinkEndpoint(f"{_TARGET_NAME}/home/seat/key#seat")
    p1, p2, p3, p4 = _patch_wire(wire)
    with p1, p2, p3, p4, pytest.raises(LinuxSecretSinkError) as excinfo:
        await endpoint.write_secret(_OPERATOR, SecretMaterial(_SENTINEL))
    assert f"reason: {reason}" in str(excinfo.value)
    assert _SENTINEL not in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_missing_ok_marker_is_a_failure_even_with_exit_zero() -> None:
    wire = _Wire(stdout="", exit_status=0)
    endpoint = LinuxSecretSinkEndpoint(f"{_TARGET_NAME}/etc/app/key#app")
    p1, p2, p3, p4 = _patch_wire(wire)
    with p1, p2, p3, p4, pytest.raises(LinuxSecretSinkError):
        await endpoint.write_secret(_OPERATOR, SecretMaterial(_SENTINEL))


# ---------------------------------------------------------------------------
# A full secret.move: Vault -> linux
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_embedding_service() -> Any:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def move_env(stub_embedding_service: Any) -> AsyncIterator[None]:
    set_default_reducer(PassThroughReducer())
    await register_secret_broker_operations(embedding_service=stub_embedding_service)
    yield
    _CONNECTOR_INSTANCE_CACHE.pop(LinuxSshConnector, None)


async def _dispatch_move(params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
        _approved=True,
    )


@pytest.mark.asyncio
async def test_vault_to_linux_move_keeps_the_value_out_of_every_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    move_env: None,
) -> None:
    install_fake_client(monkeypatch, secret={"client_secret": _SENTINEL})
    wire = _Wire(stdout="===MEHO_SINK_OK===\n")
    params = {
        "from": f"vault:tenants/{_TENANT_ID}/seat#client_secret",
        "to": f"linux:{_TARGET_NAME}/home/seat/.config/meho/client-secret#seat",
        "reason": "place the seat login secret",
    }

    p1, p2, p3, p4 = _patch_wire(wire)
    with caplog.at_level(logging.DEBUG), p1, p2, p3, p4:
        result = await _dispatch_move(params)

    assert result.status == "ok", result.error
    assert set(result.result) == {"status", "value_fingerprint", "length"}
    assert result.result["length"] == len(_SENTINEL)
    stdin = wire.stdin[0]
    assert stdin is not None and base64.b64encode(_SENTINEL.encode()).decode() in stdin

    assert _SENTINEL not in result.model_dump_json()
    assert _SENTINEL not in json.dumps(params)
    assert _SENTINEL not in caplog.text
    assert all(_SENTINEL not in c for c in wire.commands)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    move_rows = [r for r in rows if r.path == "secret.move"]
    assert len(move_rows) == 1
    assert _SENTINEL not in json.dumps(move_rows[0].payload)
    assert _SENTINEL not in json.dumps(move_rows[0].raw_payload)


@pytest.mark.asyncio
async def test_the_approval_preview_shows_only_refs() -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(),
        connector_instance=None,
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        target=None,
        params={
            "from": "vault:tenants/t/seat#client_secret",
            "to": f"linux:{_TARGET_NAME}/home/seat/key#seat",
        },
    )
    preview = await build_secret_move_preview(ctx)
    assert preview is not None
    assert preview["sink"] == {"kind": "linux", "ref": f"{_TARGET_NAME}/home/seat/key#seat"}
    assert _SENTINEL not in json.dumps(preview)


@pytest.mark.asyncio
async def test_a_move_from_a_linux_ref_fails_without_a_value(move_env: None) -> None:
    params = {
        "from": f"linux:{_TARGET_NAME}/etc/app/key#app",
        "to": f"vault:tenants/{_TENANT_ID}/x#y",
    }
    result = await _dispatch_move(params)
    assert result.status != "ok"
    assert "write-only" in json.dumps(result.model_dump(mode="json"))

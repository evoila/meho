# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for SshConnector adapter (G0.2-T4).

Coverage matrix (per Task #243 acceptance criteria + review findings):

* ``SshConnector`` class exists and is importable from the adapters package.
* Abstract class cannot be instantiated directly (ABC enforcement).
* A concrete subclass with all three ABC methods can connect and execute
  commands against an in-process asyncssh server.
* Per-target connection pool: same ``target.name`` reuses the same
  connection object; different names get distinct connections.
* Idle TTL eviction: connection idle past ``_POOL_TTL_S`` is replaced on
  next ``_connect`` call.
* Key auth path: ``target.secret_ref`` with ``ssh_private_key`` uses the
  private key for authentication.
* Password auth path: ``target.secret_ref`` with ``password`` (no
  ``ssh_private_key``) uses password authentication as fallback.
* Missing credentials: ``_auth_config`` raises ``ValueError`` immediately.
* Connection failure (wrong host / refused port) surfaces as a
  connect-time error from ``_connect``, not a command-time error.
* Command timeout: ``_run_command(..., timeout=N)`` raises
  :exc:`asyncio.TimeoutError` when the command exceeds *N* seconds.
* ``aclose()`` closes all pooled connections and empties the pool dict.
* ``aclose()`` is idempotent on a fresh connector with no pooled
  connections.
"""

from __future__ import annotations

import asyncio
import time
import types
from typing import Any
from unittest.mock import MagicMock, patch

import asyncssh
import pytest

from meho_backplane.connectors.adapters import SshConnector
from meho_backplane.connectors.adapters.ssh import _POOL_TTL_S
from meho_backplane.connectors.adapters.ssh import SshConnector as _SshConnectorDirect
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

# ---------------------------------------------------------------------------
# Shared key material (generated once per test session)
# ---------------------------------------------------------------------------

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()
_PASSWORD = "test-secret"  # NOSONAR — in-process asyncssh test server only, no real system


# ---------------------------------------------------------------------------
# In-process SSH server fixture
# ---------------------------------------------------------------------------


class _TestSSHServer(asyncssh.SSHServer):
    """Minimal in-process server accepting both auth flavours."""

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == _PASSWORD

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:
        return key == _CLIENT_PUB


async def _handle_process(process: Any) -> None:
    """Simple command handler for test server."""
    cmd: str = process.command or ""
    if cmd == "echo ok":
        process.stdout.write("ok\n")
        process.exit(0)
    elif cmd.startswith("sleep "):
        await asyncio.sleep(float(cmd.split()[1]))
        process.exit(0)
    else:
        process.stdout.write(f"{cmd}\n")
        process.exit(0)


@pytest.fixture
async def ssh_server() -> Any:
    """Yield a running in-process asyncssh server; shut it down after the test."""
    server = await asyncssh.create_server(
        _TestSSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[_SERVER_KEY],
        process_factory=_handle_process,
    )
    port: int = server.sockets[0].getsockname()[1]

    yield types.SimpleNamespace(host="127.0.0.1", port=port)

    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# Target builder helpers
# ---------------------------------------------------------------------------


def _password_target(
    name: str = "srv-01",
    *,
    host: str = "127.0.0.1",
    port: int = 22,
    username: str = "test",
) -> Any:
    return types.SimpleNamespace(
        name=name,
        host=host,
        port=port,
        secret_ref={"username": username, "password": _PASSWORD},
    )


def _key_target(
    name: str = "srv-02",
    *,
    host: str = "127.0.0.1",
    port: int = 22,
    username: str = "test",
) -> Any:
    private_key_pem = _CLIENT_KEY.export_private_key("pkcs8-pem").decode()
    return types.SimpleNamespace(
        name=name,
        host=host,
        port=port,
        secret_ref={"username": username, "ssh_private_key": private_key_pem},
    )


# ---------------------------------------------------------------------------
# Concrete subclass for testing
# ---------------------------------------------------------------------------


class _ConcreteSshConnector(SshConnector):
    """Minimal concrete subclass — all ABC methods raise NotImplementedError."""

    product = "test-ssh"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        result = await self._run_command(target, "echo ok", raw_jwt="jwt")
        from datetime import UTC, datetime

        return ProbeResult(
            ok=result.exit_status == 0,
            latency_ms=0.0,
            probed_at=datetime.now(UTC),
        )

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Import / instantiation
# ---------------------------------------------------------------------------


def test_ssh_connector_importable_from_adapters_package() -> None:
    assert SshConnector is _SshConnectorDirect


def test_ssh_connector_abstract_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        SshConnector()  # type: ignore[abstract]


def test_concrete_subclass_instantiates() -> None:
    conn = _ConcreteSshConnector()
    assert conn.product == "test-ssh"
    assert conn._connections == {}


# ---------------------------------------------------------------------------
# End-to-end against in-process server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_auth_connects_and_runs_command(ssh_server: Any) -> None:
    """Password auth path: secret_ref with username+password."""
    conn = _ConcreteSshConnector()
    target = _password_target(host=ssh_server.host, port=ssh_server.port)

    result = await conn._run_command(target, "echo ok", raw_jwt="jwt")

    assert result.exit_status == 0
    assert result.stdout is not None
    assert "ok" in result.stdout
    await conn.aclose()


@pytest.mark.asyncio
async def test_key_auth_connects_and_runs_command(ssh_server: Any) -> None:
    """Key auth path: secret_ref with ssh_private_key uses key authentication."""
    conn = _ConcreteSshConnector()
    target = _key_target(host=ssh_server.host, port=ssh_server.port)

    result = await conn._run_command(target, "echo ok", raw_jwt="jwt")

    assert result.exit_status == 0
    await conn.aclose()


@pytest.mark.asyncio
async def test_probe_via_echo_ok(ssh_server: Any) -> None:
    """Concrete subclass: probe() works via _run_command('echo ok')."""
    conn = _ConcreteSshConnector()
    target = _password_target(host=ssh_server.host, port=ssh_server.port)

    probe_result = await conn.probe(target)

    assert probe_result.ok is True
    await conn.aclose()


# ---------------------------------------------------------------------------
# Per-target connection pooling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_target_reuses_connection(ssh_server: Any) -> None:
    """Two _connect calls for the same target return the identical connection object."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="pool-test", host=ssh_server.host, port=ssh_server.port)

    ssh_a = await conn._connect(target, "jwt")
    ssh_b = await conn._connect(target, "jwt")

    assert ssh_a is ssh_b
    assert len(conn._connections) == 1
    await conn.aclose()


@pytest.mark.asyncio
async def test_different_targets_get_different_connections(ssh_server: Any) -> None:
    """Each distinct target.name gets its own SSHClientConnection."""
    conn = _ConcreteSshConnector()
    target_a = _password_target(name="srv-a", host=ssh_server.host, port=ssh_server.port)
    target_b = _password_target(name="srv-b", host=ssh_server.host, port=ssh_server.port)

    ssh_a = await conn._connect(target_a, "jwt")
    ssh_b = await conn._connect(target_b, "jwt")

    assert ssh_a is not ssh_b
    assert "srv-a" in conn._connections
    assert "srv-b" in conn._connections
    await conn.aclose()


@pytest.mark.asyncio
async def test_closed_connection_triggers_reconnect(ssh_server: Any) -> None:
    """A closed cached connection is replaced on next _connect call."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="reconnect-test", host=ssh_server.host, port=ssh_server.port)

    ssh_first = await conn._connect(target, "jwt")
    ssh_first.close()
    await ssh_first.wait_closed()

    assert ssh_first.is_closed()

    ssh_second = await conn._connect(target, "jwt")
    assert ssh_second is not ssh_first
    assert not ssh_second.is_closed()
    await conn.aclose()


# ---------------------------------------------------------------------------
# Connection failure — surfaces at connect time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_raises_at_connect_time() -> None:
    """Connection failure raises from _connect, not from a later conn.run() call."""
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="bad-host",
        host="unreachable.internal",
        port=22,
        secret_ref={"username": "u", "password": "p"},  # NOSONAR
    )

    # asyncssh.connect raises OSError on TCP failure; mock it so the test
    # doesn't depend on network behaviour or firewall rules on the test host.
    async def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Connection refused")

    with patch("asyncssh.connect", side_effect=_refuse), pytest.raises(OSError):
        await conn._connect(target, "jwt")

    # _run_command calls _connect; the error must surface before run() is called.
    with patch("asyncssh.connect", side_effect=_refuse), pytest.raises(OSError):
        await conn._run_command(target, "echo ok", raw_jwt="jwt")


# ---------------------------------------------------------------------------
# Command timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_timeout_raises_asyncio_timeout_error() -> None:
    """_run_command with a short timeout raises asyncio.TimeoutError."""
    conn = _ConcreteSshConnector()

    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False

    async def _never_finishes(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(60)

    mock_conn.run = _never_finishes

    target = types.SimpleNamespace(
        name="timeout-target",
        host="127.0.0.1",
        port=22,
        secret_ref={"username": "u", "password": "p"},  # NOSONAR
    )
    conn._connections["timeout-target"] = (mock_conn, time.monotonic())

    with pytest.raises(asyncio.TimeoutError):
        await conn._run_command(target, "sleep 60", raw_jwt="jwt", timeout=0.05)


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_all_connections_and_empties_pool(ssh_server: Any) -> None:
    """aclose() calls close()+wait_closed() on every pooled connection."""
    conn = _ConcreteSshConnector()
    target_a = _password_target(name="ac-a", host=ssh_server.host, port=ssh_server.port)
    target_b = _password_target(name="ac-b", host=ssh_server.host, port=ssh_server.port)

    await conn._connect(target_a, "jwt")
    await conn._connect(target_b, "jwt")
    assert len(conn._connections) == 2

    await conn.aclose()

    assert conn._connections == {}


@pytest.mark.asyncio
async def test_aclose_idempotent_on_empty_pool() -> None:
    """aclose() on a fresh connector is a no-op."""
    conn = _ConcreteSshConnector()
    await conn.aclose()
    assert conn._connections == {}


@pytest.mark.asyncio
async def test_aclose_skips_already_closed_connections(ssh_server: Any) -> None:
    """aclose() does not call wait_closed() on connections that are already closed."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="pre-closed", host=ssh_server.host, port=ssh_server.port)

    ssh = await conn._connect(target, "jwt")
    # Close manually before aclose()
    ssh.close()
    await ssh.wait_closed()

    # aclose() should not raise even though the connection is already closed
    await conn.aclose()
    assert conn._connections == {}


# ---------------------------------------------------------------------------
# Auth config unit tests (no real server needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_config_key_auth_returns_client_keys() -> None:
    """_auth_config picks key auth when ssh_private_key is present."""
    conn = _ConcreteSshConnector()
    private_pem = _CLIENT_KEY.export_private_key("pkcs8-pem").decode()
    target = types.SimpleNamespace(
        name="key-cfg",
        host="h",
        port=22,
        secret_ref={"username": "alice", "ssh_private_key": private_pem},
    )

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "alice"
    assert "client_keys" in cfg
    assert len(cfg["client_keys"]) == 1
    assert "password" not in cfg


@pytest.mark.asyncio
async def test_auth_config_password_auth_fallback() -> None:
    """_auth_config falls back to password when ssh_private_key is absent."""
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="pwd-cfg",
        host="h",
        port=22,
        secret_ref={"username": "bob", "password": _PASSWORD},
    )

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "bob"
    assert cfg.get("password") == _PASSWORD
    assert "client_keys" not in cfg


@pytest.mark.asyncio
async def test_auth_config_defaults_username_to_root() -> None:
    """_auth_config defaults username to 'root' when not set in secret_ref."""
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="no-user",
        host="h",
        port=22,
        secret_ref={"password": _PASSWORD},
    )

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "root"


@pytest.mark.asyncio
async def test_auth_config_raises_when_no_credentials() -> None:
    """_auth_config raises ValueError immediately when neither key nor password is present."""
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="no-creds",
        host="h",
        port=22,
        secret_ref={"username": "alice"},
    )

    with pytest.raises(ValueError, match="ssh_private_key or password"):
        await conn._auth_config(target)


# ---------------------------------------------------------------------------
# Idle TTL eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_ttl_evicts_stale_connection(ssh_server: Any) -> None:
    """Connection idle past _POOL_TTL_S is evicted and replaced on next _connect."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="ttl-test", host=ssh_server.host, port=ssh_server.port)

    ssh_first = await conn._connect(target, "jwt")

    # Backdate last_used to simulate idle expiry.
    conn._connections["ttl-test"] = (ssh_first, time.monotonic() - _POOL_TTL_S - 1.0)

    ssh_second = await conn._connect(target, "jwt")

    assert ssh_second is not ssh_first
    assert not ssh_second.is_closed()
    await conn.aclose()

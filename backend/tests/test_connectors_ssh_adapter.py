# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for SshConnector adapter (G0.2-T4).

Coverage matrix (per Task #243 acceptance criteria + review findings,
extended by #2155's Vault-path ``secret_ref`` contract):

* ``SshConnector`` class exists and is importable from the adapters package.
* Abstract class cannot be instantiated directly (ABC enforcement).
* A concrete subclass with all three ABC methods can connect and execute
  commands against an in-process asyncssh server — with ``secret_ref``
  carrying a Vault KV-v2 **path string** resolved through the
  ``_resolve_secret`` seam (#2155; the embedded-dict shape is the
  bind9 anti-shape and is rejected).
* Per-target connection pool: same ``target.name`` reuses the same
  connection object; different names get distinct connections.
* Idle TTL eviction: connection idle past ``_POOL_TTL_S`` is replaced on
  next ``_connect`` call.
* Key auth path: the resolved Vault secret with ``ssh_private_key`` uses
  the private key for authentication.
* Password auth path: the resolved Vault secret with ``password`` (no
  ``ssh_private_key``) uses password authentication as fallback.
* Missing credentials: ``_auth_config`` raises ``ValueError`` immediately.
* Dict-shaped ``secret_ref`` (the pre-#2155 anti-shape) fails with
  ``VaultCredentialsReadError`` — never ``AttributeError``.
* ``_resolve_secret`` routes through ``load_vault_secret_data`` with the
  threaded operator, and falls back to the synthesised system operator
  (fail-closed at Vault) when no operator is threaded.
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
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import asyncssh
import pytest

from meho_backplane.connectors._shared.system_operator import SYSTEM_OPERATOR_SUB
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters import SshConnector
from meho_backplane.connectors.adapters.ssh import _POOL_TTL_S
from meho_backplane.connectors.adapters.ssh import SshConnector as _SshConnectorDirect
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from tests._ssh_vault_stub import stub_ssh_vault_secrets

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
#
# ``secret_ref`` is a Vault KV-v2 path STRING (#2155). The builders
# register the secret payload under a per-name path in the live
# ``_VAULT_SECRETS`` registry the ``vault_secrets`` fixture routes
# ``SshConnector._resolve_secret`` through.
# ---------------------------------------------------------------------------

_VAULT_SECRETS: dict[str, dict[str, Any]] = {}


@pytest.fixture
def vault_secrets() -> Iterator[dict[str, dict[str, Any]]]:
    """Route the adapter's Vault resolution through the in-memory registry."""
    _VAULT_SECRETS.clear()
    with stub_ssh_vault_secrets(_VAULT_SECRETS) as registry:
        yield registry


def _password_target(
    name: str = "srv-01",
    *,
    host: str = "127.0.0.1",
    port: int = 22,
    username: str = "test",
    target_id: str | None = None,
    tenant_id: str = "00000000-0000-0000-0000-000000000000",
) -> Any:
    # Carries ``id`` and ``tenant_id`` because the connection pool keys on
    # ``target_cache_key`` (``(tenant_id, id)``); a double missing either
    # field hits ``AttributeError`` at the pool (evoila/meho#1682). Derive
    # a distinct ``id`` from ``name`` so distinct-name targets in the same
    # tenant land on distinct pool keys by default.
    secret_path = f"meho/testing/ssh/{tenant_id}/{name}"
    _VAULT_SECRETS[secret_path] = {"username": username, "password": _PASSWORD}
    return types.SimpleNamespace(
        name=name,
        host=host,
        port=port,
        id=target_id if target_id is not None else f"id-{name}",
        tenant_id=tenant_id,
        secret_ref=secret_path,
    )


def _key_target(
    name: str = "srv-02",
    *,
    host: str = "127.0.0.1",
    port: int = 22,
    username: str = "test",
    target_id: str | None = None,
    tenant_id: str = "00000000-0000-0000-0000-000000000000",
) -> Any:
    private_key_pem = _CLIENT_KEY.export_private_key("pkcs8-pem").decode()
    secret_path = f"meho/testing/ssh/{tenant_id}/{name}"
    _VAULT_SECRETS[secret_path] = {"username": username, "ssh_private_key": private_key_pem}
    return types.SimpleNamespace(
        name=name,
        host=host,
        port=port,
        id=target_id if target_id is not None else f"id-{name}",
        tenant_id=tenant_id,
        secret_ref=secret_path,
    )


# ---------------------------------------------------------------------------
# Concrete subclass for testing
# ---------------------------------------------------------------------------


class _ConcreteSshConnector(SshConnector):
    """Minimal concrete subclass — all ABC methods raise NotImplementedError."""

    product = "test-ssh"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        result = await self._run_command(target, "echo ok")
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
async def test_password_auth_connects_and_runs_command(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Password auth path: the resolved Vault secret carries username+password."""
    conn = _ConcreteSshConnector()
    target = _password_target(host=ssh_server.host, port=ssh_server.port)

    result = await conn._run_command(target, "echo ok")

    assert result.exit_status == 0
    assert result.stdout is not None
    assert "ok" in result.stdout
    await conn.aclose()


@pytest.mark.asyncio
async def test_key_auth_connects_and_runs_command(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Key auth path: the resolved Vault secret with ssh_private_key uses key auth."""
    conn = _ConcreteSshConnector()
    target = _key_target(host=ssh_server.host, port=ssh_server.port)

    result = await conn._run_command(target, "echo ok")

    assert result.exit_status == 0
    await conn.aclose()


@pytest.mark.asyncio
async def test_probe_via_echo_ok(ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]) -> None:
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
async def test_same_target_reuses_connection(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Two _connect calls for the same target return the identical connection object."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="pool-test", host=ssh_server.host, port=ssh_server.port)

    ssh_a = await conn._connect(target)
    ssh_b = await conn._connect(target)

    assert ssh_a is ssh_b
    assert len(conn._connections) == 1
    await conn.aclose()


@pytest.mark.asyncio
async def test_different_targets_get_different_connections(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Each distinct target gets its own SSHClientConnection."""
    conn = _ConcreteSshConnector()
    target_a = _password_target(name="srv-a", host=ssh_server.host, port=ssh_server.port)
    target_b = _password_target(name="srv-b", host=ssh_server.host, port=ssh_server.port)

    ssh_a = await conn._connect(target_a)
    ssh_b = await conn._connect(target_b)

    assert ssh_a is not ssh_b
    # Pool is keyed on the tenant-unique ``(tenant_id, id)`` tuple.
    assert (target_a.tenant_id, target_a.id) in conn._connections
    assert (target_b.tenant_id, target_b.id) in conn._connections
    await conn.aclose()


@pytest.mark.asyncio
async def test_same_name_different_tenant_get_distinct_connections(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Cross-tenant misrouting regression for the SSH pool (evoila/meho#1682).

    Two targets both named ``edge-router`` owned by different tenants
    must get *distinct* pooled SSH connections — a name-keyed pool would
    hand tenant B the live connection tenant A opened, routing B's
    commands onto A's session.
    """
    conn = _ConcreteSshConnector()
    target_a = _password_target(
        name="edge-router", host=ssh_server.host, port=ssh_server.port, tenant_id="tenant-a"
    )
    target_b = _password_target(
        name="edge-router", host=ssh_server.host, port=ssh_server.port, tenant_id="tenant-b"
    )

    ssh_a = await conn._connect(target_a)
    ssh_b = await conn._connect(target_b)

    assert ssh_a is not ssh_b
    assert len(conn._connections) == 2
    assert (target_a.tenant_id, target_a.id) in conn._connections
    assert (target_b.tenant_id, target_b.id) in conn._connections
    # Re-fetching B's connection returns B's, not A's (no first-writer bleed).
    assert await conn._connect(target_b) is ssh_b
    await conn.aclose()


@pytest.mark.asyncio
async def test_closed_connection_triggers_reconnect(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """A closed cached connection is replaced on next _connect call."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="reconnect-test", host=ssh_server.host, port=ssh_server.port)

    ssh_first = await conn._connect(target)
    ssh_first.close()
    await ssh_first.wait_closed()

    assert ssh_first.is_closed()

    ssh_second = await conn._connect(target)
    assert ssh_second is not ssh_first
    assert not ssh_second.is_closed()
    await conn.aclose()


# ---------------------------------------------------------------------------
# Connection failure — surfaces at connect time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_raises_at_connect_time(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """Connection failure raises from _connect, not from a later conn.run() call."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="bad-host", host="unreachable.internal", username="u")

    # asyncssh.connect raises OSError on TCP failure; mock it so the test
    # doesn't depend on network behaviour or firewall rules on the test host.
    async def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Connection refused")

    with patch("asyncssh.connect", side_effect=_refuse), pytest.raises(OSError):
        await conn._connect(target)

    # _run_command calls _connect; the error must surface before run() is called.
    with patch("asyncssh.connect", side_effect=_refuse), pytest.raises(OSError):
        await conn._run_command(target, "echo ok")


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
        id="id-timeout-target",
        tenant_id="00000000-0000-0000-0000-000000000000",
        # Pooled connection is pre-seeded below, so the Vault resolution
        # never runs — the path string is deliberately unregistered.
        secret_ref="meho/testing/ssh/unused-timeout-path",
    )
    conn._connections[(target.tenant_id, target.id)] = (mock_conn, time.monotonic())

    with pytest.raises(asyncio.TimeoutError):
        await conn._run_command(target, "sleep 60", timeout=0.05)


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_all_connections_and_empties_pool(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """aclose() calls close()+wait_closed() on every pooled connection."""
    conn = _ConcreteSshConnector()
    target_a = _password_target(name="ac-a", host=ssh_server.host, port=ssh_server.port)
    target_b = _password_target(name="ac-b", host=ssh_server.host, port=ssh_server.port)

    await conn._connect(target_a)
    await conn._connect(target_b)
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
async def test_aclose_skips_already_closed_connections(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """aclose() does not call wait_closed() on connections that are already closed."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="pre-closed", host=ssh_server.host, port=ssh_server.port)

    ssh = await conn._connect(target)
    # Close manually before aclose()
    ssh.close()
    await ssh.wait_closed()

    # aclose() should not raise even though the connection is already closed
    await conn.aclose()
    assert conn._connections == {}


# ---------------------------------------------------------------------------
# Auth config unit tests (no real server needed)
# ---------------------------------------------------------------------------


def _target_with_secret(name: str, secret: dict[str, Any]) -> Any:
    """Register *secret* at a per-name Vault path and return a matching target."""
    secret_path = f"meho/testing/ssh/cfg/{name}"
    _VAULT_SECRETS[secret_path] = secret
    return types.SimpleNamespace(name=name, host="h", port=22, secret_ref=secret_path)


@pytest.mark.asyncio
async def test_auth_config_key_auth_returns_client_keys(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """_auth_config picks key auth when ssh_private_key is present."""
    conn = _ConcreteSshConnector()
    private_pem = _CLIENT_KEY.export_private_key("pkcs8-pem").decode()
    target = _target_with_secret("key-cfg", {"username": "alice", "ssh_private_key": private_pem})

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "alice"
    assert "client_keys" in cfg
    assert len(cfg["client_keys"]) == 1
    assert "password" not in cfg


@pytest.mark.asyncio
async def test_auth_config_password_auth_fallback(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """_auth_config falls back to password when ssh_private_key is absent."""
    conn = _ConcreteSshConnector()
    target = _target_with_secret("pwd-cfg", {"username": "bob", "password": _PASSWORD})

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "bob"
    assert cfg.get("password") == _PASSWORD
    assert "client_keys" not in cfg


@pytest.mark.asyncio
async def test_auth_config_defaults_username_to_root(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """_auth_config defaults username to 'root' when the secret has no username."""
    conn = _ConcreteSshConnector()
    target = _target_with_secret("no-user", {"password": _PASSWORD})

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "root"


@pytest.mark.asyncio
async def test_auth_config_strips_credential_whitespace(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """A trailing newline on a Vault secret field never reaches asyncssh (#1474)."""
    conn = _ConcreteSshConnector()
    target = _target_with_secret(
        "trailing-newline", {"username": "carol\n", "password": _PASSWORD + "\n"}
    )

    cfg = await conn._auth_config(target)

    assert cfg["username"] == "carol"
    assert cfg["password"] == _PASSWORD


@pytest.mark.asyncio
async def test_auth_config_raises_when_no_credentials(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """_auth_config raises ValueError immediately when neither key nor password is present."""
    conn = _ConcreteSshConnector()
    target = _target_with_secret("no-creds", {"username": "alice"})

    with pytest.raises(ValueError, match="ssh_private_key or password"):
        await conn._auth_config(target)


# ---------------------------------------------------------------------------
# Vault-path secret_ref contract (#2155)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dict_secret_ref_anti_shape_fails_closed_not_attributeerror(
    vault_secrets: dict[str, dict[str, Any]],
) -> None:
    """The pre-#2155 embedded-dict secret_ref fails with the loader's error.

    The live-deploy failure this task fixes was ``AttributeError: 'str'
    object has no attribute 'get'`` — the adapter consumed the Vault
    path string as an already-materialized dict. The inverse (a target
    row still carrying an embedded dict) must fail closed through the
    credential-read error surface, never an ``AttributeError``.
    """
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="anti-shape",
        host="h",
        port=22,
        secret_ref={"username": "u", "password": "p"},  # NOSONAR — the anti-shape under test
    )

    with pytest.raises(VaultCredentialsReadError):
        await conn._auth_config(target)


@pytest.mark.asyncio
async def test_resolve_secret_threads_operator_to_vault_loader() -> None:
    """_resolve_secret forwards the threaded operator to load_vault_secret_data."""
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="op-thread", host="h", port=22, secret_ref="meho/targets/op-thread"
    )
    operator = types.SimpleNamespace(sub="operator:alice", raw_jwt="jwt-abc")
    seen: dict[str, Any] = {}

    async def _fake_loader(t: Any, op: Any, **_kwargs: Any) -> dict[str, Any]:
        seen["target"] = t
        seen["operator"] = op
        return {"username": "alice", "password": "pw"}  # NOSONAR — canned test payload

    with patch(
        "meho_backplane.connectors.adapters.ssh.load_vault_secret_data",
        side_effect=_fake_loader,
    ):
        secret = await conn._resolve_secret(target, operator)  # type: ignore[arg-type]

    assert secret == {"username": "alice", "password": "pw"}
    assert seen["target"] is target
    assert seen["operator"] is operator


@pytest.mark.asyncio
async def test_resolve_secret_without_operator_uses_synthesised_system_operator() -> None:
    """No threaded operator → the synthesised system operator (fails closed at Vault).

    ``probe()`` and readiness paths carry no operator; the adapter must
    not invent one. The synthesised system operator's placeholder JWT is
    rejected by the live Vault JWT/OIDC login, preserving the
    "system-initiated calls cannot read per-target vendor credentials"
    carve-out — asserted here by checking the greppable system ``sub``
    reaches the loader.
    """
    conn = _ConcreteSshConnector()
    target = types.SimpleNamespace(
        name="system-fallback", host="h", port=22, secret_ref="meho/targets/system"
    )
    seen: dict[str, Any] = {}

    async def _fake_loader(t: Any, op: Any, **_kwargs: Any) -> dict[str, Any]:
        seen["operator"] = op
        return {"password": "pw"}  # NOSONAR — canned test payload

    with patch(
        "meho_backplane.connectors.adapters.ssh.load_vault_secret_data",
        side_effect=_fake_loader,
    ):
        await conn._resolve_secret(target, None)

    assert seen["operator"].sub == SYSTEM_OPERATOR_SUB


# ---------------------------------------------------------------------------
# Idle TTL eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_ttl_evicts_stale_connection(
    ssh_server: Any, vault_secrets: dict[str, dict[str, Any]]
) -> None:
    """Connection idle past _POOL_TTL_S is evicted and replaced on next _connect."""
    conn = _ConcreteSshConnector()
    target = _password_target(name="ttl-test", host=ssh_server.host, port=ssh_server.port)

    ssh_first = await conn._connect(target)

    # Backdate last_used to simulate idle expiry.
    conn._connections[(target.tenant_id, target.id)] = (
        ssh_first,
        time.monotonic() - _POOL_TTL_S - 1.0,
    )

    ssh_second = await conn._connect(target)

    assert ssh_second is not ssh_first
    assert not ssh_second.is_closed()
    await conn.aclose()

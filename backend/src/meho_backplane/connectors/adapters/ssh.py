# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Abstract SSH-transport connector with asyncssh plumbing.

Every SSH-based connector (bind9, pfsense, Holodeck, etc.) inherits
:class:`SshConnector` and overrides ``fingerprint``, ``probe``, and
``execute``. Auth is centralised — vendor connectors do not override
``_auth_config``.

**Auth flavours.** ``_auth_config`` reads ``target.secret_ref``:

* **Key auth (preferred):** ``secret_ref`` carries a ``ssh_private_key``
  field (PEM-encoded text) plus ``username``; the key is parsed via
  ``asyncssh.import_private_key`` and passed as ``client_keys``.
* **Password auth (fallback):** ``secret_ref`` carries ``username`` and
  ``password``; used when ``ssh_private_key`` is absent.
* **Missing credentials:** raises :exc:`ValueError` immediately so callers
  fail fast rather than hitting an opaque asyncssh auth error.

**Per-target connection pool.** Each :class:`SshConnector` instance
maintains a per-target pool of live ``asyncssh.SSHClientConnection``
objects. Connections are cached on first use and reused until closed or
idle past ``_POOL_TTL_S`` (default 5 min). A per-target lock lets
concurrent requests to distinct targets proceed in parallel; only requests
to the *same* target serialize during the SSH handshake. SSH key exchange
is expensive — pooling matters more here than for HTTP.

**Host key checking.** ``known_hosts=None`` disables host-key verification
for v0.2; pinning is deferred to v0.2.next once a Vault-managed key store
is in place.

**Timeouts.** ``_run_command`` wraps ``conn.run()`` in
``asyncio.wait_for``; expiry raises :exc:`asyncio.TimeoutError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncssh
import structlog

from meho_backplane.connectors.base import Connector

logger = structlog.get_logger()

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any

_POOL_TTL_S: float = 300.0  # 5-minute idle eviction window


class SshConnector(Connector):
    """Abstract SSH-transport connector.

    Subclasses MUST override ``fingerprint``, ``probe``, and ``execute``.
    Auth plumbing and connection pooling are provided here.
    """

    def __init__(self) -> None:
        # Maps target.name → (SSHClientConnection, last_used monotonic timestamp).
        self._connections: dict[str, tuple[asyncssh.SSHClientConnection, float]] = {}
        # Short-lived lock protecting only _connections and _connect_locks dicts.
        self._pool_lock = asyncio.Lock()
        # Per-target connect locks — SSH handshakes for distinct targets run in parallel.
        self._connect_locks: dict[str, asyncio.Lock] = {}

    async def _auth_config(self, target: Target) -> dict[str, Any]:
        """Extract auth kwargs from ``target.secret_ref``.

        Returns ``{username, client_keys=[key]}`` for key auth, or
        ``{username, password}`` for password auth. Raises :exc:`ValueError`
        when neither ``ssh_private_key`` nor ``password`` is present.

        T5 (VaultConnector) will replace the direct dict access with a
        Vault fetch once it lands; the auth-selection logic stays here.
        """
        secret: dict[str, Any] = getattr(target, "secret_ref", {}) or {}
        username: str = secret.get("username", "root")
        private_key_text: str | None = secret.get("ssh_private_key")
        if private_key_text:
            key = asyncssh.import_private_key(private_key_text)
            return {"username": username, "client_keys": [key]}
        password: str | None = secret.get("password")
        if password:
            return {"username": username, "password": password}
        raise ValueError(
            f"target '{target.name}': secret_ref must include ssh_private_key or password"
        )

    async def _connect(self, target: Target, raw_jwt: str) -> asyncssh.SSHClientConnection:
        """Return a live SSH connection for *target*.

        Fast path (no lock): returns the cached connection when it is open
        and within the idle TTL. Slow path: acquires the per-target connect
        lock, evicts stale/closed entries, and opens a fresh connection.
        """
        # Fast path: check without acquiring any lock.
        entry = self._connections.get(target.name)
        if entry is not None:
            conn, last_used = entry
            now = time.monotonic()
            if not conn.is_closed() and (now - last_used) <= _POOL_TTL_S:
                self._connections[target.name] = (conn, now)
                return conn

        # Get or create the per-target connect lock.
        async with self._pool_lock:
            if target.name not in self._connect_locks:
                self._connect_locks[target.name] = asyncio.Lock()
            t_lock = self._connect_locks[target.name]

        async with t_lock:
            # Double-check under per-target lock.
            now = time.monotonic()
            entry = self._connections.get(target.name)
            if entry is not None:
                conn, last_used = entry
                if not conn.is_closed() and (now - last_used) <= _POOL_TTL_S:
                    self._connections[target.name] = (conn, now)
                    return conn
                # Evict closed or idle-expired entry.
                if not conn.is_closed():
                    conn.close()
                    await conn.wait_closed()
                del self._connections[target.name]

            auth_kwargs = await self._auth_config(target)
            conn = await asyncssh.connect(
                target.host,
                port=target.port or 22,
                username=auth_kwargs["username"],
                client_keys=auth_kwargs.get("client_keys"),
                password=auth_kwargs.get("password"),
                known_hosts=None,
            )
            self._connections[target.name] = (conn, time.monotonic())
            logger.info("ssh_connected", target=target.name, host=target.host)
            return conn

    async def _run_command(
        self,
        target: Target,
        cmd: str,
        *,
        raw_jwt: str,
        timeout: float = 30.0,
    ) -> asyncssh.SSHCompletedProcess:
        """Run *cmd* on *target* via the pooled SSH connection.

        Raises :exc:`asyncio.TimeoutError` when *timeout* is exceeded.
        """
        conn = await self._connect(target, raw_jwt)
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        logger.info(
            "ssh_command_executed",
            target=target.name,
            cmd_len=len(cmd),
            exit_code=result.exit_status,
        )
        return result

    async def aclose(self) -> None:
        """Close all pooled connections. Called by lifespan or cleanup."""
        async with self._pool_lock:
            to_close = list(self._connections.values())
            self._connections.clear()
            self._connect_locks.clear()
        for conn, _ in to_close:
            if not conn.is_closed():
                conn.close()
                await conn.wait_closed()

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

**Per-target connection caching.** Each :class:`SshConnector` instance
owns a dict of live ``asyncssh.SSHClientConnection`` objects keyed by
``target.name``. The connection is created on first use and reused until
closed or evicted. SSH key exchange is expensive — pooling matters more
here than for HTTP.

**Host key checking.** ``known_hosts=None`` disables host-key verification
for v0.2; pinning is deferred to v0.2.next once a Vault-managed key store
is in place.

**Timeouts.** ``_run_command`` wraps ``conn.run()`` in
``asyncio.wait_for``; expiry raises :exc:`asyncio.TimeoutError`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncssh
import structlog

from meho_backplane.connectors.base import Connector

logger = structlog.get_logger()

# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any


class SshConnector(Connector):
    """Abstract SSH-transport connector.

    Subclasses MUST override ``fingerprint``, ``probe``, and ``execute``.
    Auth plumbing and connection pooling are provided here.
    """

    def __init__(self) -> None:
        self._connections: dict[str, asyncssh.SSHClientConnection] = {}
        self._lock = asyncio.Lock()

    async def _auth_config(self, target: Target) -> dict[str, Any]:
        """Extract auth kwargs from ``target.secret_ref``.

        Returns ``{username, client_keys=[key]}`` for key auth, or
        ``{username, password}`` for password auth (fallback).

        T5 (VaultConnector) will replace the direct dict access with a
        Vault fetch once it lands; the auth-selection logic stays here.
        """
        secret: dict[str, Any] = getattr(target, "secret_ref", {}) or {}
        username: str = secret.get("username", "root")
        private_key_text: str | None = secret.get("ssh_private_key")
        if private_key_text:
            key = asyncssh.import_private_key(private_key_text)
            return {"username": username, "client_keys": [key]}
        return {"username": username, "password": secret.get("password")}

    async def _connect(self, target: Target, raw_jwt: str) -> asyncssh.SSHClientConnection:
        """Return the cached SSH connection for *target*, creating it if needed."""
        async with self._lock:
            existing = self._connections.get(target.name)
            if existing is not None and not existing.is_closed():
                return existing

            auth_kwargs = await self._auth_config(target)
            conn = await asyncssh.connect(
                target.host,
                port=target.port or 22,
                username=auth_kwargs["username"],
                client_keys=auth_kwargs.get("client_keys"),
                password=auth_kwargs.get("password"),
                known_hosts=None,
            )
            self._connections[target.name] = conn
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
            cmd=cmd,
            exit_code=result.exit_status,
        )
        return result

    async def aclose(self) -> None:
        """Close all pooled connections. Called by lifespan or cleanup."""
        async with self._lock:
            for conn in self._connections.values():
                if not conn.is_closed():
                    conn.close()
                    await conn.wait_closed()
            self._connections.clear()

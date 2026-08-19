# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the vcf-installer connector.

The hand-rolled :class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
reads service-account credentials from the target's Vault path and exchanges
them for a session token at ``POST /v1/tokens`` (the ``session_login_token``
scheme — ``TokenCreationSpec`` in, ``TokenPair.accessToken`` out), sending it as
``Authorization: Bearer <accessToken>`` on every subsequent request.

The credential fetch (Vault path → ``{"username": ..., "password": ...}`` dict)
is split behind a narrow :class:`InstallerCredentialsLoader` callable so
production reuses the live operator-context KV-v2 read
(:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`,
rubric **State 2**, ``shared_service_account`` only) while unit / integration
tests inject a loader returning a pre-built dict. This is the same split the
sddc-manager connector uses — the Installer is the second member of the
``session_login_token`` scheme.

:class:`InstallerTargetLike` captures the minimum target shape read: ``name``
(cache key), ``host``, ``port``, ``secret_ref`` (the Vault path), and
``auth_model`` (boundary-checked). The concrete ``Target`` model in
:mod:`meho_backplane.targets` (G0.3 #224) satisfies it structurally.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "InstallerCredentialsLoader",
    "InstallerTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`InstallerCredentialsLoader` returns — the
    ``{username, password}`` pair POSTed to ``/v1/tokens``."""

    username: str
    password: str


@runtime_checkable
class InstallerTargetLike(Protocol):
    """Minimum target shape :class:`InstallerConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` satisfies it unchanged. ``auth_model`` is
    boundary-checked so a ``per_user`` / ``impersonation`` target raises a clear
    error rather than silently authenticating as the shared service account.
    ``secret_ref`` is the Vault path the loader resolves; ``str | None`` matches
    the nullable column (an unset ref is rejected inside the loader, never a bare
    ``KeyError``). ``id`` / ``tenant_id`` form the tenant-unique cache key so two
    same-named targets in different tenants never share a cached credential.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


InstallerCredentialsLoader = Callable[[InstallerTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to ``{"username", "password"}``.

Invoked once per target (first-use) by
:meth:`InstallerConnector._load_credentials`, caching the dict under the
tenant-unique key. The ``operator`` carries the frozen
:class:`~meho_backplane.auth.operator.Operator` so the live loader reads the
per-target secret under the operator's identity via
``vault_client_for_operator(operator)``.
"""


async def load_credentials_from_vault(
    target: InstallerTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret under the operator's identity
    and returns the service-account ``{"username", "password"}`` pair the
    connector exchanges for a session token at ``POST /v1/tokens``. Delegates to
    the shared :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper so the read, the no-secret-in-logs discipline, and the two-phase error
    contract (``VaultCredentialsReadError`` for read-phase, ``VaultClientError``
    for login-phase) are defined once. A custom
    :class:`InstallerCredentialsLoader` can be injected via ``credentials_loader``
    on :class:`InstallerConnector` (tests do exactly that); this default is what
    production targets at rubric State 2 use.
    """
    return await load_basic_credentials(target, operator)

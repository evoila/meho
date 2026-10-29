# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading and target shape for the FreeIPA connector.

:class:`~meho_backplane.connectors.freeipa.connector.FreeipaConnector` logs in
with ``POST /ipa/session/login_password``: an IPA user name and password. The
credential read sits behind the narrow :class:`FreeipaCredentialsLoader`
callable, so production reads Vault under the operator's identity while tests
pass a static loader. Same shape as :mod:`meho_backplane.connectors.salt.session`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "FreeipaCredentialsLoader",
    "FreeipaTargetLike",
    "load_credentials_from_vault",
]


@runtime_checkable
class FreeipaTargetLike(Protocol):
    """Minimum target shape :class:`FreeipaConnector` reads.

    The concrete ``Target`` model in :mod:`meho_backplane.targets` satisfies
    this Protocol unchanged. ``id`` / ``tenant_id`` form the tenant-unique
    cache key. ``secret_ref`` is the Vault path the loader reads. ``host`` is
    the IPA server; FreeIPA checks that the ``Referer`` header names the
    server's own host name, so a target that connects by IP address or by an
    alias sets ``tls_server_name`` to that name.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


FreeipaCredentialsLoader = Callable[[FreeipaTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector calls it on
every login: on first use against a target, and again after a ``401`` made
it drop the cached session.
"""


async def load_credentials_from_vault(
    target: FreeipaTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader: an operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` under the operator's identity and returns the
    ``{"username": ..., "password": ...}`` pair. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`,
    so the no-secret-in-logs rule and the error contract are defined once.
    """
    return await load_basic_credentials(target, operator)

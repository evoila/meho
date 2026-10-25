# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading and target shape for the salt connector.

:class:`~meho_backplane.connectors.salt.connector.SaltConnector` logs in to
the RaaS HTTP bridge of Aria Automation Config (VMware Salt) with
``GET /account/login`` and HTTP Basic: a RaaS user. The credential fetch
sits behind the narrow :class:`SaltSessionLoader` callable, so production
reads Vault under the operator's identity while tests inject a static
loader. Same shape as :mod:`meho_backplane.connectors.srm.session`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "SaltSessionLoader",
    "SaltTargetLike",
    "load_session_credentials_from_vault",
]


@runtime_checkable
class SaltTargetLike(Protocol):
    """Minimum target shape :class:`SaltConnector` reads.

    The concrete ``Target`` model in :mod:`meho_backplane.targets`
    satisfies this Protocol unchanged. ``id`` / ``tenant_id`` form the
    tenant-unique session cache key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`).
    ``secret_ref`` is the Vault path the loader resolves; ``port`` is
    optional (RaaS listens on 443).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


SaltSessionLoader = Callable[[SaltTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector calls the
loader on every login: on first use against a target, and again after a
``401`` made it drop the cached session.
"""


async def load_session_credentials_from_vault(
    target: SaltTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader: live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` under the operator's identity and returns
    the ``{"username": ..., "password": ...}`` pair the connector sends as
    HTTP Basic. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper, so the no-secret-in-logs rule and the error contract are
    defined once for every REST connector.
    """
    return await load_basic_credentials(target, operator)

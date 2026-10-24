# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading and target shape for the srm connector.

:class:`~meho_backplane.connectors.srm.connector.SrmConnector` logs in to
the Site Recovery Manager REST API with ``POST /api/rest/srm/v2/session``
and HTTP Basic: the vCenter single sign-on account of the SRM site. The
credential fetch sits behind the narrow :class:`SrmSessionLoader`
callable, so production reads Vault under the operator's identity while
tests inject a static loader. Same shape as
:mod:`meho_backplane.connectors.hcx.session`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "SrmSessionLoader",
    "SrmTargetLike",
    "load_session_credentials_from_vault",
]


@runtime_checkable
class SrmTargetLike(Protocol):
    """Minimum target shape :class:`SrmConnector` reads.

    The concrete ``Target`` model in :mod:`meho_backplane.targets`
    satisfies this Protocol unchanged. ``id`` / ``tenant_id`` form the
    tenant-unique session cache key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`),
    so two targets that share one address never share a session.
    ``secret_ref`` is the Vault path the loader resolves; ``port`` is
    optional (the appliance listens on 443).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


SrmSessionLoader = Callable[[SrmTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector calls the
loader on every login: on first use against a target, and again after a
``401`` made it drop the cached session.
"""


async def load_session_credentials_from_vault(
    target: SrmTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader: live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` under the operator's identity and returns
    the ``{"username": ..., "password": ...}`` pair the connector sends as
    HTTP Basic to ``POST /api/rest/srm/v2/session``. Delegates to the
    shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper, so the no-secret-in-logs rule and the error contract are
    defined once for every REST connector.
    """
    return await load_basic_credentials(target, operator)

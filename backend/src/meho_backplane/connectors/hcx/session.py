# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading for the hcx connector.

:class:`~meho_backplane.connectors.hcx.connector.HcxConnector` logs in to
an HCX Manager with ``POST /hybridity/api/sessions`` (a JSON body with
``username`` and ``password``: the vCenter single sign-on account that is
registered with the HCX Manager). The credential fetch is split out behind
the narrow :class:`HcxSessionLoader` callable so production uses the live
operator-context Vault read while tests inject a static loader. Same shape
as :mod:`meho_backplane.connectors.nsx.session`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "HcxSessionLoader",
    "HcxTargetLike",
    "is_acceptable_auth_model",
    "load_session_credentials_from_vault",
]


def is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (a target
    whose ``auth_model`` column is not set yet). Every other value is
    rejected by :meth:`HcxConnector.auth_headers`.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


@runtime_checkable
class HcxTargetLike(Protocol):
    """Minimum target shape :class:`HcxConnector` reads.

    The concrete ``Target`` model in :mod:`meho_backplane.targets`
    satisfies this Protocol unchanged. ``id`` / ``tenant_id`` form the
    tenant-unique cache key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    for the session-token cache. ``secret_ref`` is the Vault path the
    loader resolves; ``port`` is optional (the HCX Manager listens on 443).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


HcxSessionLoader = Callable[[HcxTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector calls the
loader on every login: on first use against a target, and again after a
``401`` made it drop the cached session token.
"""


async def load_session_credentials_from_vault(
    target: HcxTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader: live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` under the operator's identity and returns
    the ``{"username": ..., "password": ...}`` pair the connector sends to
    ``POST /hybridity/api/sessions``. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper, so the no-secret-in-logs discipline and the error contract are
    defined once for every REST connector.
    """
    return await load_basic_credentials(target, operator)

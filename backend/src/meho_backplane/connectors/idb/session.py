# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target settings and login secret of the ``idb`` connector.

Both products log in with an OAuth 2.0 client (client credentials). An admin
makes the client once, in the Workspace ONE Access console or with the
Identity Broker OAuth client API. MEHO keeps its id and secret in the
credential store at ``target.secret_ref`` (Vault KV-v2 by default), as the
fields ``client_id`` and ``client_secret``. The secret is read under the
operator's identity and never enters a log, a result, an audit row or an
error text.

Target settings (``target.extras``):

* ``tenant`` (Identity Broker 9.x only, optional) -- the tenant name in every
  Identity Broker path (``/acs/t/{tenant}/...``). Default ``CUSTOMER``, the
  default tenant of VCF Identity Broker.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)

__all__ = [
    "DEFAULT_TENANT",
    "IdbConfigError",
    "IdbCredentials",
    "IdbCredentialsLoader",
    "IdbTargetLike",
    "load_credentials_from_vault",
    "parse_credentials",
    "resolve_tenant",
]

DEFAULT_TENANT: Final[str] = "CUSTOMER"

#: Tenant names are short words (letters, digits, dash, underscore).
TENANT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class IdbConfigError(ValueError):
    """The target or its secret is not set up the way the connector needs."""


@runtime_checkable
class IdbTargetLike(Protocol):
    """The target fields the connector reads."""

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    extras: Mapping[str, Any] | None


@dataclass(frozen=True)
class IdbCredentials:
    """The OAuth client. ``repr`` never shows a value."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


IdbCredentialsLoader = Callable[[IdbTargetLike, Operator], Awaitable[IdbCredentials]]


def resolve_tenant(target: IdbTargetLike) -> str:
    """Return ``extras.tenant`` of *target*, or ``CUSTOMER`` when it is not set."""
    extras = target.extras if isinstance(target.extras, Mapping) else {}
    tenant = extras.get("tenant") or DEFAULT_TENANT
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        raise IdbConfigError(
            f"Identity Broker target {target.name!r}: extras.tenant must be the tenant name "
            "(letters, digits, dash, underscore; at most 64 characters), for example CUSTOMER"
        )
    return tenant


def parse_credentials(data: Mapping[str, object], *, target_name: str) -> IdbCredentials:
    """Turn the stored secret into :class:`IdbCredentials`.

    Raises :class:`IdbConfigError` naming the field names that are present,
    never a value.
    """
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if (
        isinstance(client_id, str | int)
        and isinstance(client_secret, str | int)
        and strip_credential_value(client_id)
        and strip_credential_value(client_secret)
    ):
        return IdbCredentials(
            client_id=strip_credential_value(client_id),
            client_secret=strip_credential_value(client_secret),
        )
    raise IdbConfigError(
        f"target {target_name!r}: the secret needs the fields 'client_id' and "
        f"'client_secret' (an OAuth client); it has {sorted(str(key) for key in data)}"
    )


async def load_credentials_from_vault(target: IdbTargetLike, operator: Operator) -> IdbCredentials:
    """Read the secret at ``target.secret_ref`` under *operator*'s identity."""
    data = await load_vault_secret_data(target, operator)
    return parse_credentials(data, target_name=target.name)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target settings and login secret of the phpIPAM connector.

Target settings (``target.extras``):

* ``app_id`` (required) -- the id of the API app in phpIPAM
  (Administration > API). It is part of every URL: ``/api/{app_id}/...``.
* ``base_path`` (optional) -- the sub-path phpIPAM runs under, for example
  ``/phpipam``. Empty by default.

The login secret lives in MEHO's credential store at ``target.secret_ref``
(Vault KV-v2 by default). Two shapes are accepted:

* ``{"token": "..."}`` -- the app code of an API app with the security
  "SSL with App code token". MEHO sends it as the ``phpipam-token`` header.
* ``{"username": "...", "password": "..."}`` -- a phpIPAM user, for an API app
  with the security "SSL with User token". MEHO logs in with
  ``POST /api/{app_id}/user/`` and sends the token it gets back.

When both are present, the app code wins. The secret is read under the
operator's identity and never enters a log, a result, an audit row or an
error text.
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
    "PhpipamConfigError",
    "PhpipamCredentials",
    "PhpipamCredentialsLoader",
    "PhpipamTargetConfig",
    "PhpipamTargetLike",
    "load_credentials_from_vault",
    "parse_credentials",
    "resolve_target_config",
]

#: phpIPAM app ids are short names (letters, digits, dash, underscore).
APP_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: A sub-path such as ``/phpipam``: segments of safe characters, no ``..``.
BASE_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^(/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*$")


class PhpipamConfigError(ValueError):
    """The target or its secret is not set up the way the connector needs."""


@runtime_checkable
class PhpipamTargetLike(Protocol):
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
class PhpipamTargetConfig:
    """The checked target settings."""

    app_id: str
    base_path: str


@dataclass(frozen=True)
class PhpipamCredentials:
    """The login secret. ``repr`` never shows a value."""

    app_token: str | None = field(default=None, repr=False)
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def mode(self) -> str:
        """``app_token`` or ``user_token``: how MEHO gets the token it sends."""
        return "app_token" if self.app_token else "user_token"


PhpipamCredentialsLoader = Callable[[PhpipamTargetLike, Operator], Awaitable[PhpipamCredentials]]


def resolve_target_config(target: PhpipamTargetLike) -> PhpipamTargetConfig:
    """Read and check ``extras.app_id`` and ``extras.base_path`` of *target*."""
    extras = target.extras if isinstance(target.extras, Mapping) else {}
    app_id = extras.get("app_id")
    if not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id):
        raise PhpipamConfigError(
            f"phpIPAM target {target.name!r}: extras.app_id must be the id of the phpIPAM "
            "API app (letters, digits, dash, underscore; at most 64 characters)"
        )
    base_path = extras.get("base_path") or ""
    if not isinstance(base_path, str):
        base_path = ""
    base_path = base_path.rstrip("/")
    if not BASE_PATH_RE.fullmatch(base_path):
        raise PhpipamConfigError(
            f"phpIPAM target {target.name!r}: extras.base_path must be a sub-path such as "
            "'/phpipam' (letters, digits, dot, dash, underscore, tilde)"
        )
    return PhpipamTargetConfig(app_id=app_id, base_path=base_path)


def parse_credentials(data: Mapping[str, object], *, target_name: str) -> PhpipamCredentials:
    """Turn the stored secret into :class:`PhpipamCredentials`.

    Raises :class:`PhpipamConfigError` naming the field names that are
    missing, never a value.
    """
    token = data.get("token")
    if isinstance(token, str | int) and strip_credential_value(token):
        return PhpipamCredentials(app_token=strip_credential_value(token))
    username = data.get("username")
    password = data.get("password")
    if (
        isinstance(username, str | int)
        and isinstance(password, str | int)
        and strip_credential_value(username)
        and strip_credential_value(password)
    ):
        return PhpipamCredentials(
            username=strip_credential_value(username),
            password=strip_credential_value(password),
        )
    raise PhpipamConfigError(
        f"phpIPAM target {target_name!r}: the secret needs the field 'token' (the API app "
        "code) or the fields 'username' and 'password' (a phpIPAM user); it has "
        f"{sorted(str(key) for key in data)}"
    )


async def load_credentials_from_vault(
    target: PhpipamTargetLike, operator: Operator
) -> PhpipamCredentials:
    """Read the secret at ``target.secret_ref`` under *operator*'s identity."""
    data = await load_vault_secret_data(target, operator)
    return parse_credentials(data, target_name=target.name)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read a password the call names by its Vault path, and keep it out of answers.

An Installer operation never takes a password as a value. The caller names a
Vault KV-v2 path (``secret_ref``, with an optional ``secret_mount`` and
``secret_key``). The handler reads it under the caller's own identity just
before the vendor call. A Vault refusal raises
:class:`~meho_backplane.connectors._shared.vault_creds.SecretRefReadError`
(the dispatcher answers ``vault_credentials_read_error``). A secret with no
text under the key raises :class:`SecretValueError`. No message holds a value.

:func:`hide_secret` replaces a vendor text that holds one of the passwords,
so a vendor error that echoes the request cannot leak it.
:func:`hide_secrets_everywhere` does the same for every text in a whole
answer, at any depth.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "DEFAULT_SECRET_KEY",
    "SecretValueError",
    "hide_secret",
    "hide_secrets_everywhere",
    "read_secret_ref",
]

#: The key in a Vault secret that holds the password, unless the call names another.
DEFAULT_SECRET_KEY: Final = "password"

_HIDDEN: Final = "[MEHO hid this text: it held a password from Vault]"


class SecretValueError(ValueError):
    """The Vault secret a call names has no text under its key."""


@dataclass(slots=True)
class _RefTarget:
    """Points the shared Vault read at the path the call names."""

    name: str
    host: str
    secret_ref: str | None


async def read_secret_ref(
    operator: Operator,
    target: InstallerTargetLike,
    ref: Mapping[str, Any],
    *,
    param: str,
) -> str:
    """Read the password at ``ref["secret_ref"]`` under the caller's identity.

    *param* names the parameter in error messages (for example
    ``proxy.password_secret_ref``).
    """
    secret_ref = str(ref.get("secret_ref") or "").strip()
    mount = str(ref.get("secret_mount") or DEFAULT_KV_MOUNT).strip()
    key = str(ref.get("secret_key") or DEFAULT_SECRET_KEY).strip()
    if not secret_ref:
        raise SecretValueError(f"{param} is empty: name the Vault path that holds the password")
    data = await load_param_secret_data(
        _RefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param=param,
        mount=mount,
    )
    value = data.get(key) if isinstance(data, dict) else None
    password = strip_credential_value(value) if isinstance(value, str) else ""
    if not password:
        raise SecretValueError(
            f"the Vault secret named by {param} (mount={mount!r}) has no text under the key "
            f"{key!r}. Store the password under that key. Nothing was sent."
        )
    return password


def hide_secret(text: Any, secrets: Iterable[str]) -> Any:
    """*text*, or a fixed note when it holds one of *secrets*."""
    if not isinstance(text, str):
        return text
    if any(secret and secret in text for secret in secrets):
        return _HIDDEN
    return text


def hide_secrets_everywhere(value: Any, secrets: Iterable[str]) -> Any:
    """A copy of *value* with every text that holds one of *secrets* replaced, at any depth."""
    found = [secret for secret in secrets if secret]
    if not found:
        return value
    return _hide_everywhere(value, found)


def _hide_everywhere(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return hide_secret(value, secrets)
    if isinstance(value, dict):
        return {key: _hide_everywhere(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide_everywhere(item, secrets) for item in value]
    return value

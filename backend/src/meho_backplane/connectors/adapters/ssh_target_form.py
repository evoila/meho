# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login fields of an SSH connector, as it declares them for the target form.

:meth:`SshConnector._auth_config` reads the same secret fields for every
connector that does not override it. This module builds the ways to log in
from those field names, so each connector class declares them in one line
and keeps its own wording of the user name help.

A connector that does not override ``_auth_config`` has two ways: ``key``
(``username``, ``ssh_private_key``, ``known_hosts``) and ``password``
(``username``, ``password``, ``known_hosts``). The adapter also accepts
``private_key`` in place of ``ssh_private_key``; the form shows the main name.
``known_hosts`` is required because the adapter refuses a host whose key it
cannot check.

The core only reads the declaration. It does not change how a login works.
"""

from __future__ import annotations

from meho_backplane.connectors.target_form import CredentialField, CredentialWay

__all__ = ["SSH_DEFAULT_PORT", "WINDOWS_USER_HELP", "ssh_credential_ways", "ssh_known_hosts_field"]

#: The port the adapter dials when the target has none (``target.port or 22``).
SSH_DEFAULT_PORT = 22

#: The user name help of a Windows host. The adapter logs in as ``root`` when
#: the secret has no ``username``, and a Windows host has no such user.
WINDOWS_USER_HELP = (
    "The Windows user to log in as. MEHO uses root when you leave it out, "
    "and a Windows host has no such user, so set it."
)

_SUDO_HELP = (
    "The password that sudo asks for, for the operations that change the host. "
    "Without it MEHO uses the login password."
)


def ssh_known_hosts_field() -> CredentialField:
    """The host key field every SSH way shares."""
    return CredentialField(
        name="known_hosts",
        label="Known hosts",
        help="The host key line of the server. MEHO needs it to trust the host.",
        hidden=False,
        multiline=True,
    )


def _username_field(user_help: str) -> CredentialField:
    return CredentialField(
        name="username", label="User name", help=user_help, required=False, hidden=False
    )


def ssh_credential_ways(
    *, user_help: str, sudo_password: bool = False
) -> tuple[CredentialWay, ...]:
    """The two ways of a connector that uses the adapter's own ``_auth_config``.

    ``user_help`` says which user to log in as. With ``sudo_password`` the key
    way also offers the optional ``sudo_password`` field, which the operations
    that run a command under sudo read before they fall back to ``password``.
    """
    key_fields: tuple[CredentialField, ...] = (
        _username_field(user_help),
        CredentialField(
            name="ssh_private_key",
            label="Private key",
            help="The private key of the user, as text.",
            multiline=True,
        ),
        ssh_known_hosts_field(),
    )
    if sudo_password:
        key_fields = (
            *key_fields,
            CredentialField(
                name="sudo_password", label="Sudo password", help=_SUDO_HELP, required=False
            ),
        )
    return (
        CredentialWay(key="key", label="SSH key", fields=key_fields),
        CredentialWay(
            key="password",
            label="Password",
            fields=(
                _username_field(user_help),
                CredentialField(
                    name="password", label="Password", help="The password of the user."
                ),
                ssh_known_hosts_field(),
            ),
        ),
    )

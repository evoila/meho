# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading, login mode and target shape for the vro connector.

:class:`~meho_backplane.connectors.vro.connector.VroConnector` logs in to
the Orchestrator REST API (``/vco/api``) in one of three ways, chosen per
target with ``extras["login"]``:

* ``basic`` (the default) -- HTTP Basic on every call, with the account
  read from Vault. This is the login of an Orchestrator that uses vSphere
  single sign-on (Orchestrator 8.x on its own). On VCF 9 it works only
  when the Orchestrator's basic login over single sign-on is switched on.
* ``vcfa-session`` -- the VCF 9 login. A VCF 9 Orchestrator signs people
  in through VCF Automation, so the connector gets a VCF Automation
  session token for ``<user>@<extras.org>`` with the shared login
  (:func:`meho_backplane.connectors._shared.vcfa_session.vcfa_session_login`)
  and sends ``Authorization: Bearer <token>``. It logs in again 60 seconds
  before the token ends and once after a ``401``.
* ``automation`` -- the Aria Automation token. An Orchestrator inside Aria
  Automation 8.x takes the same bearer token as the Automation services,
  so the connector runs the Aria Automation login
  (:func:`meho_backplane.connectors.vra8._auth.vra8_login`) against the
  same address and sends ``Authorization: Bearer <token>``.

All modes read the same ``{"username", "password"}`` pair from Vault
through the narrow :data:`VroCredentialsLoader` callable, so tests inject
a static loader.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors._shared.vcfa_session import LOGIN_VCFA_SESSION

__all__ = [
    "LOGIN_AUTOMATION",
    "LOGIN_BASIC",
    "LOGIN_MODES",
    "LOGIN_VCFA_SESSION",
    "VroCredentialsLoader",
    "VroTargetLike",
    "load_credentials_from_vault",
    "login_mode",
    "vcfa_org",
]

#: HTTP Basic on every call (vSphere single sign-on). The default.
LOGIN_BASIC: Final = "basic"
#: The Aria Automation bearer token (Orchestrator inside Aria Automation 8.x).
LOGIN_AUTOMATION: Final = "automation"
LOGIN_MODES: Final[tuple[str, ...]] = (LOGIN_BASIC, LOGIN_AUTOMATION, LOGIN_VCFA_SESSION)


@runtime_checkable
class VroTargetLike(Protocol):
    """Minimum target shape :class:`VroConnector` reads.

    The concrete ``Target`` model satisfies it unchanged. ``id`` and
    ``tenant_id`` form the tenant-unique login cache key. ``extras`` may
    hold ``login`` (see :func:`login_mode`) and, for the ``automation``
    login, the optional Aria Automation ``domain``.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


VroCredentialsLoader = Callable[[VroTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to ``{"username", "password"}``."""


def login_mode(target: Any) -> str:
    """Return the target's login mode: ``basic`` (default), ``automation`` or ``vcfa-session``.

    Raises :exc:`ValueError` for any other ``extras["login"]`` value, so a
    typo never falls back to a login the operator did not choose.
    """
    extras = getattr(target, "extras", None)
    raw = extras.get("login") if isinstance(extras, dict) else None
    if raw is None or raw == "":
        return LOGIN_BASIC
    if raw not in LOGIN_MODES:
        raise ValueError(
            f"vro target {getattr(target, 'name', '?')!r} has extras.login={raw!r}; "
            f"use one of {list(LOGIN_MODES)}"
        )
    return str(raw)


def vcfa_org(target: Any) -> str | None:
    """Return ``extras["org"]``, the VCF Automation org of the login user, or ``None``."""
    extras = getattr(target, "extras", None)
    org = extras.get("org") if isinstance(extras, dict) else None
    return org.strip() if isinstance(org, str) and org.strip() else None


async def load_credentials_from_vault(target: VroTargetLike, operator: Operator) -> dict[str, str]:
    """Default loader: read ``target.secret_ref`` from Vault under the operator's identity."""
    return await load_basic_credentials(target, operator)

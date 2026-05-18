# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading for the nsx connector.

The hand-rolled :class:`~meho_backplane.connectors.nsx.connector.NsxConnector`
trades operator-context Vault reads to a session cookie + XSRF token via
NSX's ``POST /api/session/create`` endpoint (form-encoded
``j_username`` / ``j_password``). The credential fetch (Vault path ->
service-account ``{"username": ..., "password": ...}`` dict) is split out
behind a narrow :class:`NsxSessionLoader` callable so:

* Production deploys override the default loader at construction time
  once G0.3 (#224) lands the operator-context Vault read path.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests against a recorded-fixture or live NSX target pass a
  loader that yields the appropriate service-account credentials.

The default loader, :func:`load_session_credentials_from_vault`, raises
:exc:`NotImplementedError` until G0.3 (#224) merges. Mirrors the shape
:func:`~meho_backplane.connectors.vmware_rest.session.load_session_credentials_from_vault`
established for :class:`VmwareRestConnector` -- once the Target model +
operator-context Vault reads land, both loaders pick up the concrete
implementation in a single follow-up commit.

The :class:`NsxTargetLike` Protocol captures the minimum target shape
the connector reads: ``name`` (for the per-target session-token cache
key), ``host``, ``port`` (forwarded to :meth:`HttpConnector._base_url`),
``secret_ref`` (the Vault path the loader resolves), and ``auth_model``
(checked by :meth:`NsxConnector.auth_headers` to reject ``per_user`` /
``impersonation`` targets at the boundary). Once G0.3 ships its
concrete ``Target`` model in :mod:`meho_backplane.targets`, the model
satisfies this Protocol structurally -- no edits here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "NsxSessionLoader",
    "NsxTargetLike",
    "SessionCredentials",
    "load_session_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`NsxSessionLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values map to the form-encoded body
    ``j_username`` / ``j_password`` NSX's ``POST /api/session/create``
    expects; nothing else is read.
    """

    username: str
    password: str


@runtime_checkable
class NsxTargetLike(Protocol):
    """Minimum target shape :class:`NsxConnector` reads.

    Structural Protocol -- once G0.3 (#224) lands the concrete
    ``Target`` model in :mod:`meho_backplane.targets`, that model
    satisfies this Protocol unchanged. ``auth_model`` is checked at the
    boundary so a target tagged ``per_user`` or ``impersonation`` raises
    a clear error rather than silently authenticating as the shared
    service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``port`` is optional --
    NSX manager defaults to 443 and
    :meth:`HttpConnector._base_url` already handles the
    ``port is None or 443`` case correctly.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None


NsxSessionLoader = Callable[[NsxTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's :meth:`NsxConnector._session_token` invokes the loader
on every session-establish (first use against a target, and again after
a 401-driven invalidation). The return type is the looser
``dict[str, str]`` (not :class:`SessionCredentials`) because Python
:class:`Protocol` instances aren't runtime-constructible without a
matching class -- production code returns a plain dict and the
connector reads ``creds["username"]`` / ``creds["password"]`` by key.
"""


async def load_session_credentials_from_vault(
    target: NsxTargetLike,
) -> dict[str, str]:
    """Default credential loader -- Vault read by ``target.secret_ref``.

    Stubbed until G0.3 (#224) lands the ``Target`` model and the
    operator-context Vault read path. Mirrors
    :func:`~meho_backplane.connectors.vmware_rest.session.load_session_credentials_from_vault`'s
    ``NotImplementedError`` stub so the wiring shape is stable: a
    production caller without an explicit loader override receives a
    clear error rather than a silent fallback or a hallucinated
    credential pair.

    Once G0.3 lands, this function becomes the live implementation that
    reads the ``nsx/<target.name>`` Vault path and returns the parsed
    ``{"username": ..., "password": ...}`` dict. Until then, tests and
    any acceptance harness inject a custom :class:`NsxSessionLoader` on
    connector construction.
    """
    raise NotImplementedError(
        "load_session_credentials_from_vault requires G0.3 Target model + "
        f"operator-context Vault read; target={target.name!r} "
        f"secret_ref={target.secret_ref!r}. Inject a custom session_loader "
        "on NsxConnector until G0.3 lands."
    )

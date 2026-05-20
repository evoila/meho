# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the harbor connector.

The hand-rolled :class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
reads service-account credentials from the target's Vault path and sends
them as HTTP Basic auth on every request — no session token is established;
the ``Authorization: Basic`` header is recomputed from the cached credentials
on each call.

The credential fetch (Vault path → ``{"username": ..., "password": ...}``
dict) is split out behind a narrow :class:`HarborCredentialsLoader` callable
so:

* Production deploys override the default loader at construction time once
  G0.3 (#224) lands the operator-context Vault read path.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the appropriate service-account
  credentials.

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until G0.3 (#224) merges. Mirrors the shape
:func:`~meho_backplane.connectors.sddc_manager.session.load_credentials_from_vault`
established for :class:`SddcManagerConnector`.

Harbor supports two account forms:

* **Admin account**: plain username (e.g. ``"admin"``).
* **Robot account**: Harbor-formatted username (e.g. ``"robot$project+name"``
  for a project-scoped robot or ``"robot$name"`` for a system-level robot).

Both forms are stored verbatim in Vault under the target's ``secret_ref``
path. The connector sends the stored username as-is in the Basic auth header;
no reformatting is applied here.

The :class:`HarborTargetLike` Protocol captures the minimum target shape the
connector reads: ``name`` (for the per-target credential cache key), ``host``,
``port`` (forwarded to :meth:`HttpConnector._base_url`), ``secret_ref`` (the
Vault path the loader resolves), and ``auth_model`` (checked at the
boundary). Unlike the SDDC Manager protocol, no ``sso_realm`` field is
needed — Harbor's Basic auth header carries ``username:password`` directly
with no realm suffix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "HarborCredentialsLoader",
    "HarborTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`HarborCredentialsLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values map to the Basic auth components the
    connector sends on every Harbor API request.
    """

    username: str
    password: str


@runtime_checkable
class HarborTargetLike(Protocol):
    """Minimum target shape :class:`HarborConnector` reads.

    Structural Protocol — once G0.3 (#224) lands the concrete ``Target``
    model in :mod:`meho_backplane.targets`, that model satisfies this
    Protocol unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``port`` is optional —
    Harbor defaults to 443 and :meth:`HttpConnector._base_url` already
    handles the ``port is None or 443`` case correctly.

    No ``sso_realm`` field — Harbor sends ``username:password`` as-is;
    no realm suffix is appended.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None


HarborCredentialsLoader = Callable[[HarborTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's :meth:`HarborConnector._load_credentials` invokes the
loader exactly once per target (first-use), caching the resulting dict under
``target.name``. The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class — production code
returns a plain dict and the connector reads ``creds["username"]`` /
``creds["password"]`` by key.
"""


async def load_credentials_from_vault(
    target: HarborTargetLike,
) -> dict[str, str]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    Stubbed until G0.3 (#224) lands the ``Target`` model and the
    operator-context Vault read path. Mirrors
    :func:`~meho_backplane.connectors.sddc_manager.session.load_credentials_from_vault`'s
    ``NotImplementedError`` stub so the wiring shape is stable: a production
    caller without an explicit loader override receives a clear error rather
    than a silent fallback or a hallucinated credential pair.

    Once G0.3 lands, this function becomes the live implementation that reads
    the ``harbor/<target.name>`` Vault path and returns the parsed
    ``{"username": ..., "password": ...}`` dict. Until then, tests and any
    acceptance harness inject a custom :class:`HarborCredentialsLoader` on
    connector construction.
    """
    raise NotImplementedError(
        "load_credentials_from_vault requires G0.3 Target model + "
        f"operator-context Vault read; target={target.name!r} "
        f"secret_ref={target.secret_ref!r}. Inject a custom credentials_loader "
        "on HarborConnector until G0.3 lands."
    )

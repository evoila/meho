# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the sddc-manager connector.

The hand-rolled :class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`
reads service-account credentials from the target's Vault path and sends
them as HTTP Basic auth on every request — no session token is established;
the ``Authorization: Basic`` header is recomputed from the cached credentials
on each call.

The credential fetch (Vault path → ``{"username": ..., "password": ...}``
dict) is split out behind a narrow :class:`SddcCredentialsLoader` callable
so:

* Production deploys override the default loader at construction time once
  G0.3 (#224) lands the operator-context Vault read path.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the appropriate service-account
  credentials.

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until G0.3 (#224) merges. Mirrors the shape
:func:`~meho_backplane.connectors.vmware_rest.session.load_session_credentials_from_vault`
established for :class:`VmwareRestConnector` — once the Target model +
operator-context Vault reads land, all loaders pick up the concrete
implementation in a single follow-up commit.

The :class:`SddcTargetLike` Protocol captures the minimum target shape the
connector reads: ``name`` (for the per-target credential cache key), ``host``,
``port`` (forwarded to :meth:`HttpConnector._base_url`), ``secret_ref`` (the
Vault path the loader resolves), ``auth_model`` (checked at the boundary),
and ``sso_realm`` (the SSO domain appended to the username in the Basic auth
header, defaulting to ``"vsphere.local"``). Once G0.3 ships its concrete
``Target`` model in :mod:`meho_backplane.targets`, the model satisfies this
Protocol structurally — no edits here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "SddcCredentialsLoader",
    "SddcTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`SddcCredentialsLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values map to the Basic auth components the
    connector sends on every SDDC Manager API request; nothing else is
    read.
    """

    username: str
    password: str


@runtime_checkable
class SddcTargetLike(Protocol):
    """Minimum target shape :class:`SddcManagerConnector` reads.

    Structural Protocol — once G0.3 (#224) lands the concrete ``Target``
    model in :mod:`meho_backplane.targets`, that model satisfies this
    Protocol unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``port`` is optional —
    SDDC Manager defaults to 443 and :meth:`HttpConnector._base_url`
    already handles the ``port is None or 443`` case correctly.

    ``sso_realm`` is the vSphere SSO domain appended to ``username`` when
    constructing the Basic auth header (``username@sso_realm``). Defaults
    to ``"vsphere.local"`` per the consumer wrapper contract; operators
    managing a custom domain override this at the target level.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None
    sso_realm: str


SddcCredentialsLoader = Callable[[SddcTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's :meth:`SddcManagerConnector._load_credentials` invokes the
loader exactly once per target (first-use), caching the resulting dict under
``target.name``. The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class — production code
returns a plain dict and the connector reads ``creds["username"]`` /
``creds["password"]`` by key.
"""


async def load_credentials_from_vault(
    target: SddcTargetLike,
) -> dict[str, str]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    Stubbed until G0.3 (#224) lands the ``Target`` model and the
    operator-context Vault read path. Mirrors
    :func:`~meho_backplane.connectors.vmware_rest.session.load_session_credentials_from_vault`'s
    ``NotImplementedError`` stub so the wiring shape is stable: a production
    caller without an explicit loader override receives a clear error rather
    than a silent fallback or a hallucinated credential pair.

    Once G0.3 lands, this function becomes the live implementation that reads
    the ``sddc-manager/<target.name>`` Vault path and returns the parsed
    ``{"username": ..., "password": ...}`` dict. Until then, tests and any
    acceptance harness inject a custom :class:`SddcCredentialsLoader` on
    connector construction.
    """
    raise NotImplementedError(
        "load_credentials_from_vault requires G0.3 Target model + "
        f"operator-context Vault read; target={target.name!r} "
        f"secret_ref={target.secret_ref!r}. Inject a custom credentials_loader "
        "on SddcManagerConnector until G0.3 lands."
    )

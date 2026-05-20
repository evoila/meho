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
  the operator-context per-target Vault credential read is wired for this
  connector (tracked under the open
  `Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_).
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the appropriate service-account
  credentials.

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until the live read lands. Mirrors the shape
:func:`~meho_backplane.connectors.vmware_rest.session.load_session_credentials_from_vault`
established for :class:`VmwareRestConnector` — both stubs are tracked
under the open Goal #214 and switch over together.

The :class:`SddcTargetLike` Protocol captures the minimum target shape the
connector reads: ``name`` (for the per-target credential cache key), ``host``,
``port`` (forwarded to :meth:`HttpConnector._base_url`), ``secret_ref`` (the
Vault path the loader resolves), ``auth_model`` (checked at the boundary),
and ``sso_realm`` (the SSO domain appended to the username in the Basic auth
header, defaulting to ``"vsphere.local"``). The concrete ``Target`` model in
:mod:`meho_backplane.targets` (G0.3 #224 — closed) satisfies this Protocol
structurally; no edits here.
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

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224 — closed) satisfies this
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

    Deliberate stub: the operator-context per-target Vault credential read
    is not yet wired for the SDDC Manager connector. Raising
    :exc:`NotImplementedError` here keeps the wiring shape stable — a
    production caller without an override receives a clear error rather
    than a silent fallback or a hallucinated credential pair. The supported
    workaround is to inject a custom ``credentials_loader`` on
    ``SddcManagerConnector`` at construction time. The live read is
    tracked under the open Goal #214 (Connector parity).

    Once the read lands, this function becomes the live implementation that
    reads the ``sddc-manager/<target.name>`` Vault path and returns the
    parsed ``{"username": ..., "password": ...}`` dict.
    """
    raise NotImplementedError(
        "load_credentials_from_vault is a deliberate stub: the operator-context "
        "per-target Vault credential read is not yet wired for the SDDC Manager "
        f"connector; target={target.name!r} secret_ref={target.secret_ref!r}. "
        "Workaround: inject a custom credentials_loader on SddcManagerConnector. "
        "Tracked under open Goal #214 (Connector parity): "
        "https://github.com/evoila/meho/issues/214"
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading for the vmware-rest connector.

The hand-rolled :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
trades operator-context Vault reads to a session token via vCenter's
``POST /api/session`` endpoint. The credential fetch (Vault path -> service-
account ``{"username": ..., "password": ...}`` dict) is split out behind a
narrow :class:`VsphereSessionLoader` callable so:

* Production deploys can override the default loader at construction time
  once G0.3 (#224) lands the operator-context Vault read path.
* Unit tests inject their own (mock) loader that returns a pre-built dict.
* Integration tests against vcsim pass a loader that yields the
  simulator's hard-coded ``user``/``pass`` credentials.

The default loader, :func:`load_session_credentials_from_vault`, raises
:exc:`NotImplementedError` until G0.3 (#224) merges. Mirrors the
shape :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`
already established for :class:`KubernetesConnector` — once the Target
model + operator-context Vault reads land, both loaders pick up the
concrete implementation in a single follow-up commit.

The :class:`VsphereTargetLike` Protocol captures the minimum target shape
the connector reads: ``name`` (for the per-target session cache key),
``host``, ``port`` (forwarded to :meth:`HttpConnector._base_url`),
``secret_ref`` (the Vault path the loader resolves), and ``auth_model``
(checked by :meth:`VmwareRestConnector.auth_headers` to reject
``per_user`` / ``impersonation`` targets at the boundary). Once G0.3 ships
its concrete ``Target`` model in :mod:`meho_backplane.targets`, the
model satisfies this Protocol structurally — no edits here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "SessionCredentials",
    "VsphereSessionLoader",
    "VsphereTargetLike",
    "load_session_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :func:`VsphereSessionLoader` returns.

    Captured as a TypedDict-style Protocol rather than a concrete
    :class:`dict` so the type checker can flag a loader that forgets a
    key. Keys are deliberately the two HTTP basic-auth components vCenter
    expects on ``POST /api/session``; nothing else is read.
    """

    username: str
    password: str


@runtime_checkable
class VsphereTargetLike(Protocol):
    """Minimum target shape :class:`VmwareRestConnector` reads.

    Structural Protocol — once G0.3 (#224) lands the concrete ``Target``
    model in :mod:`meho_backplane.targets`, that model satisfies this
    Protocol unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``port`` is optional —
    vCenter defaults to 443 and :meth:`HttpConnector._base_url` already
    handles the ``port is None or 443`` case correctly.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None


VsphereSessionLoader = Callable[[VsphereTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's :meth:`VmwareRestConnector._session_token` invokes the
loader exactly once per target (first-use), caching the resulting session
token under ``target.name``. The return type is the looser
``dict[str, str]`` (not :class:`SessionCredentials`) because Python
:class:`Protocol` instances aren't runtime-constructible without a
matching class — production code returns a plain dict and the connector
reads ``creds["username"]`` / ``creds["password"]`` by key.
"""


async def load_session_credentials_from_vault(
    target: VsphereTargetLike,
) -> dict[str, str]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    Stubbed until G0.3 (#224) lands the ``Target`` model and the
    operator-context Vault read path. Mirrors
    :func:`~meho_backplane.connectors.kubernetes.kubeconfig.load_kubeconfig_from_vault`'s
    NotImplementedError stub so the wiring shape is stable: a production
    caller without an explicit loader override receives a clear error
    rather than a silent fallback or a hallucinated credential pair.

    Once G0.3 lands, this function becomes the live implementation that
    reads the ``vsphere/<target.name>`` Vault path and returns the
    parsed ``{"username": ..., "password": ...}`` dict. Until then,
    tests and any acceptance harness inject a custom
    :class:`VsphereSessionLoader` on connector construction.
    """
    raise NotImplementedError(
        "load_session_credentials_from_vault requires G0.3 Target model + "
        f"operator-context Vault read; target={target.name!r} "
        f"secret_ref={target.secret_ref!r}. Inject a custom session_loader "
        "on VmwareRestConnector until G0.3 lands."
    )

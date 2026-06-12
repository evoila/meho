# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the Hetzner Robot connector.

The hand-rolled :class:`~meho_backplane.connectors.hetzner_robot.connector.HetznerRobotConnector`
reads service-account credentials from the target's Vault path and sends
them as HTTP Basic auth on every request.  No session token is established;
the ``Authorization: Basic`` header is recomputed from the cached credentials
on each call.

The Hetzner Robot API authenticates with a **Webservice user** — a separate
account that must be created in the Robot portal and is distinct from the
Robot login user. Credentials are stored verbatim in Vault under the
target's ``secret_ref`` path as ``{"username": ..., "password": ...}``.

**Critical: IP-block protection.** Hetzner Robot blocks the source IP for
10 minutes after 3 failed 401 responses from that IP.  Any credential
mismatch must therefore surface as an immediate hard error — never a retry.
The loader is injectable so unit tests supply canned credentials; the
production path (live Vault read) is a deliberate stub until Goal #214
lands.

The :class:`HetznerRobotTargetLike` Protocol captures the minimum target
shape the connector reads: ``name`` (per-target cache key), ``host``,
``port`` (forwarded to :meth:`HttpConnector._base_url`), ``secret_ref``
(the Vault path the loader resolves), and ``auth_model`` (checked at the
auth boundary). No ``sso_realm`` field — Hetzner Robot sends
``username:password`` directly in the Basic header.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "HetznerRobotCredentialsLoader",
    "HetznerRobotTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`HetznerRobotCredentialsLoader` returns.

    Both keys map to the HTTP Basic auth components sent on every request.
    """

    username: str
    password: str


@runtime_checkable
class HetznerRobotTargetLike(Protocol):
    """Minimum target shape :class:`HetznerRobotConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224 — closed) satisfies this
    Protocol unchanged.  ``auth_model`` is checked at the auth boundary so a
    target tagged ``per_user`` raises a clear error rather than silently
    authenticating as the shared service account.

    ``secret_ref`` is the Vault path resolved to ``{"username", "password"}``.
    ``port`` is optional — the Robot API is HTTPS/443 and
    :meth:`HttpConnector._base_url` already handles the ``port is None or
    443`` case correctly.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    the credential cache uses, so two same-named targets in different
    tenants never share cached credentials (#1642/#1672).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None


HetznerRobotCredentialsLoader = Callable[[HetznerRobotTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's :meth:`HetznerRobotConnector._load_credentials` invokes the
loader exactly once per target (first-use), caching the resulting dict under
``target.name``.  The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class.
"""


async def load_credentials_from_vault(
    target: HetznerRobotTargetLike,
) -> dict[str, str]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    Deliberate stub: the operator-context per-target Vault credential read is
    not yet wired for the Hetzner Robot connector.  Raising
    :exc:`NotImplementedError` here keeps the wiring shape stable — a
    production caller without an override receives a clear error rather than
    a silent fallback or a hallucinated credential pair.  The live read is
    tracked under the open Goal #214 (Connector parity).
    """
    raise NotImplementedError(
        "load_credentials_from_vault is a deliberate stub: the operator-context "
        "per-target Vault credential read is not yet wired for the Hetzner Robot "
        f"connector; target={target.name!r} secret_ref={target.secret_ref!r}. "
        "Workaround: inject a custom credentials_loader on HetznerRobotConnector. "
        "Tracked under open Goal #214 (Connector parity): "
        "https://github.com/evoila/meho/issues/214"
    )

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

The default loader, :func:`load_credentials_from_vault`, performs the
**live** operator-context KV-v2 read by delegating to the shared
:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
helper (#2079) — the same read every REST connector's default loader uses
(harbor, vmware, sddc). The Webservice-user credential is read under the
operator's Vault identity (``vault_client_for_operator(operator)``), so
Vault RBAC and audit attribute the read to the operator. The loader is
injectable so unit tests supply canned credentials and integration tests
supply the appropriate Webservice-user pair.

**Critical: IP-block protection.** Hetzner Robot blocks the source IP for
10 minutes after 3 failed 401 responses from that IP.  Any credential
mismatch must therefore surface as an immediate hard error — never a retry.

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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

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
    It is ``str | None`` to match the concrete ``Target.secret_ref`` column
    (nullable) and the shared
    :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the default loader forwards to; an unset ``secret_ref`` is rejected with
    a clear error inside the loader (an unconfigured target), never a bare
    ``KeyError``. ``port`` is optional — the Robot API is HTTPS/443 and
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
    secret_ref: str | None
    auth_model: str | None


HetznerRobotCredentialsLoader = Callable[
    [HetznerRobotTargetLike, Operator], Awaitable[dict[str, str]]
]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector's
:meth:`HetznerRobotConnector._load_credentials` invokes the loader exactly
once per target (first-use), caching the resulting dict under the
tenant-unique ``(tenant_id, id)`` cache key.  The return type is the looser
``dict[str, str]`` (not :class:`SessionCredentials`) because Python
:class:`Protocol` instances aren't runtime-constructible without a matching
class.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live loader
reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the operator-context read every
REST connector uses.
"""


async def load_credentials_from_vault(
    target: HetznerRobotTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to Vault's
    JWT/OIDC auth method) and returns the dedicated Webservice-user
    ``{"username": ..., "password": ...}`` pair the connector sends as HTTP
    Basic auth on every Robot Webservice request. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper so the read, the no-secret-in-logs discipline, and the two-phase
    error contract are defined once for every REST connector — this loader is
    the thin hetzner-specific entry point.

    The Webservice user is **distinct from the Robot portal login user** and
    must be created separately in the Robot portal; its credentials are stored
    verbatim in Vault under the target's ``secret_ref``.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a system-initiated
      call, unset ``target.secret_ref``, a malformed KV-v2 payload, or a
      missing ``username``/``password`` field). Never a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and subclasses) —
      login-phase failure (Vault unreachable, role denied). Propagated
      verbatim so callers can distinguish login from read.

    A custom :class:`HetznerRobotCredentialsLoader` can still be injected via
    ``credentials_loader`` on :class:`HetznerRobotConnector` (tests do exactly
    that); this default is what production targets at rubric State 2
    (``shared_service_account``) use.
    """
    return await load_basic_credentials(target, operator)

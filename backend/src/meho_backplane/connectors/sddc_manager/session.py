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

* Production deploys reuse the default loader, which now performs the
  **live** operator-context KV-v2 read via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper (G3.10-T1 #945).
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the appropriate service-account
  credentials.

The default loader, :func:`load_credentials_from_vault`, performs the live
operator-context KV-v2 read by delegating to
:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`.
This is the rubric **State 2** wiring (`shared_service_account` only) per
`Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_.

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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

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
    :class:`SessionCredentials`-shaped dict. It is ``str | None`` to
    match the concrete ``Target.secret_ref`` column (nullable) and the
    shared :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the loader forwards to; an unset ``secret_ref`` is rejected with a
    clear error inside the loader (an unconfigured target), never a
    bare ``KeyError``. ``port`` is optional — SDDC Manager defaults to
    443 and :meth:`HttpConnector._base_url` already handles the
    ``port is None or 443`` case correctly.

    ``sso_realm`` is the vSphere SSO domain appended to ``username`` when
    constructing the Basic auth header (``username@sso_realm``). Defaults
    to ``"vsphere.local"`` per the consumer wrapper contract; operators
    managing a custom domain override this at the target level.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    sso_realm: str


SddcCredentialsLoader = Callable[[SddcTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector's
:meth:`SddcManagerConnector._load_credentials` invokes the loader
exactly once per target (first-use), caching the resulting dict under
``target.name``. The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class — production code
returns a plain dict and the connector reads ``creds["username"]`` /
``creds["password"]`` by key.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the locked decision in
[docs/architecture/connector-auth.md](docs/architecture/connector-auth.md).
"""


async def load_credentials_from_vault(
    target: SddcTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) and returns the service-account
    ``{"username": ..., "password": ...}`` pair the connector sends as
    HTTP Basic auth on every SDDC Manager API call. Delegates to the
    shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2 #941) so the read, the no-secret-in-logs discipline,
    and the two-phase error contract are defined once for every REST
    connector — this loader is the thin sddc-manager-specific entry
    point.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``username``/``password`` field).
      Never a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and its
      subclasses) — login-phase failure (Vault unreachable, role
      denied). Propagated verbatim so callers can distinguish login
      from read.

    A custom :class:`SddcCredentialsLoader` can still be injected via
    ``credentials_loader`` on :class:`SddcManagerConnector` (tests do
    exactly that); this default is what production targets at rubric
    State 2 (`shared_service_account`) use.
    """
    return await load_basic_credentials(target, operator)

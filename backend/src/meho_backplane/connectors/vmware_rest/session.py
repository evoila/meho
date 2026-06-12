# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-credential loading for the vmware-rest connector.

The hand-rolled :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
trades operator-context Vault reads to a session token via vCenter's
``POST /api/session`` endpoint. The credential fetch (Vault path -> service-
account ``{"username": ..., "password": ...}`` dict) is split out behind a
narrow :class:`VsphereSessionLoader` callable so:

* Production deploys can override the default loader at construction time
  with the operator-context Vault read path.
* Unit tests inject their own (mock) loader that returns a pre-built dict.
* Integration tests against vcsim pass a loader that yields the
  simulator's hard-coded ``user``/``pass`` credentials.

The default loader, :func:`load_session_credentials_from_vault`, performs
the **live** operator-context KV-v2 read: it forwards the operator's
validated Keycloak JWT to Vault and reads ``target.secret_ref`` for the
service-account ``{"username", "password"}`` pair. It delegates to the
shared :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
helper (G3.9-T2) so the read, the no-secret-in-logs discipline, and the
two-phase error contract live in one place every REST connector reuses.
This is the rubric **State 2** wiring (`shared_service_account` only) per
`Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_.

The :class:`VsphereTargetLike` Protocol captures the minimum target shape
the connector reads: ``name`` (for the per-target session cache key),
``host``, ``port`` (forwarded to :meth:`HttpConnector._base_url`),
``secret_ref`` (the Vault path the loader resolves), and ``auth_model``
(checked by :meth:`VmwareRestConnector.auth_headers` to reject
``per_user`` / ``impersonation`` targets at the boundary). Any concrete
``Target`` model in :mod:`meho_backplane.targets` that exposes these
attributes satisfies this Protocol structurally — no edits here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

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

    Structural Protocol — any concrete ``Target`` model in
    :mod:`meho_backplane.targets` that exposes these attributes
    satisfies it unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. It is ``str | None`` to
    match the concrete ``Target.secret_ref`` column (nullable) and the
    shared :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the loader forwards to; an unset ``secret_ref`` is rejected with a
    clear error inside the loader (an unconfigured target), never a bare
    ``KeyError``. ``port`` is optional — vCenter defaults to 443 and
    :meth:`HttpConnector._base_url` already handles the
    ``port is None or 443`` case correctly.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    the session-token cache uses, so two same-named targets in different
    tenants never share a cached session (#1642/#1672).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


VsphereSessionLoader = Callable[[VsphereTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector's
:meth:`VmwareRestConnector._session_token` invokes the loader exactly
once per target (first-use), caching the resulting session token under
``target.name``. The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class — production code
returns a plain dict and the connector reads ``creds["username"]`` /
``creds["password"]`` by key.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader (G3.9-T3) can read the per-target secret under the operator's
identity via ``vault_client_for_operator(operator)`` — the locked
decision in
[docs/architecture/connector-auth.md](docs/architecture/connector-auth.md).
"""


async def load_session_credentials_from_vault(
    target: VsphereTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) and returns the service-account
    ``{"username": ..., "password": ...}`` pair the connector POSTs to
    ``/api/session``. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2) so the read, the no-secret-in-logs discipline, and
    the two-phase error contract are defined once for every REST
    connector — this loader is the thin vmware-specific entry point.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``username``/``password`` field). Never
      a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and its
      subclasses) — login-phase failure (Vault unreachable, role denied).
      Propagated verbatim so callers can distinguish login from read.

    A custom :class:`VsphereSessionLoader` can still be injected via
    ``session_loader`` on ``VmwareRestConnector`` (tests do exactly that);
    this default is what production targets at rubric State 2
    (`shared_service_account`) use.
    """
    return await load_basic_credentials(target, operator)

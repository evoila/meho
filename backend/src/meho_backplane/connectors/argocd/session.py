# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the argocd connector.

The hand-rolled :class:`~meho_backplane.connectors.argocd.connector.ArgoCdConnector`
reads a single ``argocd-server`` API **bearer token** from the target's Vault
path and sends it as ``Authorization: Bearer <token>`` on every request — no
session is established and no login round-trip is performed; the header is
recomputed from the cached token on each call.

ArgoCD's auth model differs from Harbor's HTTP Basic and vmware's
session-POST: ``argocd-server`` accepts a JWT bearer token minted as an
ArgoCD project/account API token (``argocd account generate-token`` or a
``project`` token). That single opaque string is the credential — there is
no username component. It is stored under the target's ``secret_ref`` as a
KV-v2 secret with a ``token`` field.

The credential fetch (Vault path → ``{"token": ...}`` dict) is split out
behind a narrow :class:`ArgoCdCredentialsLoader` callable so:

* Production deploys reuse the default loader, which performs the **live**
  operator-context KV-v2 read via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper (G3.9-T2 #941 / G3.10-T1 #945 precedent), requesting just the
  ``token`` field.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the appropriate API token.

The :class:`ArgoCdTargetLike` Protocol captures the minimum target shape the
connector reads: ``name`` (for the per-target credential cache key), ``host``,
``port`` (forwarded to :meth:`HttpConnector._base_url`), ``secret_ref`` (the
Vault path the loader resolves), and ``auth_model`` (checked at the
boundary). The bearer token is a shared service-account credential, so the
target's ``auth_model`` is ``shared_service_account`` — the same boundary the
Harbor and SDDC Manager connectors enforce.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "ARGOCD_TOKEN_FIELD",
    "ArgoCdCredentialsLoader",
    "ArgoCdTargetLike",
    "load_credentials_from_vault",
]

#: The single KV-v2 secret field the connector reads — the ArgoCD API
#: bearer token. Kept as a module constant so the connector, the default
#: loader, and the tests share one source of truth for the field name an
#: operator must store under ``target.secret_ref``.
ARGOCD_TOKEN_FIELD = "token"


@runtime_checkable
class ArgoCdTargetLike(Protocol):
    """Minimum target shape :class:`ArgoCdConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224) satisfies this Protocol
    unchanged. ``auth_model`` is checked at the boundary so a target
    tagged ``per_user`` or ``impersonation`` raises a clear error rather
    than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a dict carrying
    the ``token`` field. It is ``str | None`` to match the concrete
    ``Target.secret_ref`` column (nullable) and the shared
    :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the loader forwards to; an unset ``secret_ref`` is rejected with a
    clear error inside the loader (an unconfigured target), never a bare
    ``KeyError``. ``port`` is optional — ``argocd-server`` defaults to 443
    and :meth:`HttpConnector._base_url` already handles the
    ``port is None or 443`` case correctly.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    the credential cache uses, so two same-named targets in different
    tenants never share a cached token (#1642/#1672).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


ArgoCdCredentialsLoader = Callable[[ArgoCdTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns a dict carrying the ``token`` key (the ArgoCD API bearer token).
The connector's :meth:`ArgoCdConnector._load_credentials` invokes the
loader exactly once per target (first-use), caching the resulting dict
under ``target.name``. The return type is the looser ``dict[str, str]``
(not a TypedDict) because production code returns a plain dict and the
connector reads ``creds["token"]`` by key.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the locked decision in
``docs/architecture/connector-auth.md``.
"""


async def load_credentials_from_vault(
    target: ArgoCdTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) and returns the ``{"token": ...}`` pair
    the connector sends as ``Authorization: Bearer <token>`` on every
    ``argocd-server`` API call. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2 #941) with ``fields=("token",)`` so the read, the
    no-secret-in-logs discipline, and the two-phase error contract are
    defined once for every REST connector — this loader is the thin
    argocd-specific entry point.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``token`` field). Never a bare
      ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and its
      subclasses) — login-phase failure (Vault unreachable, role denied).
      Propagated verbatim so callers can distinguish login from read.

    A custom :class:`ArgoCdCredentialsLoader` can still be injected via
    ``credentials_loader`` on :class:`ArgoCdConnector` (tests do exactly
    that); this default is what production targets at rubric State 2
    (``shared_service_account``) use.
    """
    return await load_basic_credentials(target, operator, fields=(ARGOCD_TOKEN_FIELD,))

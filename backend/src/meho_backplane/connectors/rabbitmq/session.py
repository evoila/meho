# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the RabbitMQ connector (#2233).

The hand-rolled
:class:`~meho_backplane.connectors.rabbitmq.connector.RabbitMqConnector`
reads a ``username`` / ``password`` pair from the target's Vault path and
sends it as HTTP Basic auth (``Authorization: Basic <base64>``) on every
Management HTTP API request. RabbitMQ's Management plugin authenticates
each request with HTTP Basic against a broker user (typically a user
carrying the ``monitoring`` tag for read-only observability, or
``policymaker`` when shovel/federation runtime parameters must be read).

The credential fetch (Vault path → ``{"username", "password"}`` dict) is
split out behind a narrow :class:`RabbitMqCredentialsLoader` callable so:

* Production deploys reuse the default loader, which performs the **live**
  operator-context KV-v2 read via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper (the Harbor / ArgoCD precedent) requesting the
  ``username`` + ``password`` fields.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests pass a loader that yields the broker's monitoring
  user credentials.

The :class:`RabbitMqTargetLike` Protocol captures the minimum target
shape the connector reads: ``id`` / ``tenant_id`` (the tenant-unique
credential cache key), ``name``, ``host``, ``port`` (forwarded to
:meth:`HttpConnector._base_url`; the Management API listens on 15672 for
HTTP and 15671 for HTTPS), ``secret_ref`` (the Vault path the loader
resolves), and ``auth_model`` (checked at the boundary). The Basic
credential is a shared service-account credential, so the target's
``auth_model`` is ``shared_service_account`` — the same boundary the
Harbor and ArgoCD connectors enforce.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "RABBITMQ_CREDENTIAL_FIELDS",
    "RabbitMqCredentialsLoader",
    "RabbitMqTargetLike",
    "load_credentials_from_vault",
]

#: The KV-v2 secret fields the connector reads — the RabbitMQ Management
#: HTTP API user's ``username`` and ``password``. Kept as a module
#: constant so the connector, the default loader, and the tests share one
#: source of truth for the field names an operator stores under
#: ``target.secret_ref``.
RABBITMQ_CREDENTIAL_FIELDS: tuple[str, str] = ("username", "password")


@runtime_checkable
class RabbitMqTargetLike(Protocol):
    """Minimum target shape :class:`RabbitMqConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` satisfies it unchanged. ``auth_model``
    is checked at the boundary so a target tagged ``per_user`` or
    ``impersonation`` raises a clear error rather than silently
    authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a dict
    carrying the ``username`` + ``password`` fields. It is ``str | None``
    to match the concrete nullable ``Target.secret_ref`` column and the
    shared
    :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the loader forwards to; an unset ``secret_ref`` is rejected with a
    clear error inside the loader, never a bare ``KeyError``. ``port`` is
    optional — :meth:`HttpConnector._base_url` handles the
    ``port is None or 443`` case; an operator points the target at the
    Management port (15672 / 15671) explicitly.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
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


RabbitMqCredentialsLoader = Callable[[RabbitMqTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns a dict carrying the ``username`` + ``password`` keys (the
RabbitMQ Management HTTP API Basic-auth credential). The connector's
:meth:`RabbitMqConnector._load_credentials` invokes the loader exactly
once per target (first-use), caching the resulting dict under the
tenant-unique cache key.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the locked decision in
``docs/architecture/connector-auth.md``.
"""


async def load_credentials_from_vault(
    target: RabbitMqTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) and returns the
    ``{"username": ..., "password": ...}`` pair the connector sends as
    ``Authorization: Basic <base64(username:password)>`` on every
    Management HTTP API call. Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper with ``fields=("username", "password")`` so the read, the
    no-secret-in-logs discipline, and the two-phase error contract are
    defined once for every REST connector — this loader is the thin
    rabbitmq-specific entry point.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``username`` / ``password`` field).
      Never a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and
      subclasses) — login-phase failure (Vault unreachable, role denied).
      Propagated verbatim so callers can distinguish login from read.

    A custom :class:`RabbitMqCredentialsLoader` can still be injected via
    ``credentials_loader`` on :class:`RabbitMqConnector` (tests do exactly
    that); this default is what production ``shared_service_account``
    targets use.
    """
    return await load_basic_credentials(target, operator, fields=RABBITMQ_CREDENTIAL_FIELDS)

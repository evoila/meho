# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading + target shape for the vcf-fleet connector.

The hand-rolled
:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
authenticates with HTTP Basic against Fleet's **local LCM user store** —
typically the ``admin@local`` account. There is no SSO federation; the
stored username is sent verbatim in the Basic auth header (consumer
wrapper ``scripts/vcf-fleet.sh`` header, 2026-05-21: *"HTTP Basic against
Fleet's own LCM-local user store (typical username `admin@local`). Fleet
does NOT federate with vCenter SSO out of the box."*).

The connector reuses the shared scaffolding in
:mod:`meho_backplane.connectors._shared.vcf_auth` (#841):

* :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
  for the load-once-per-target ``{"username": str, "password": str}``
  dict with the missing-key → :exc:`RuntimeError` contract.
* :func:`~meho_backplane.connectors._shared.vcf_auth.basic_auth_header`
  for the ``Authorization: Basic`` header value.
* :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
  for the ``shared_service_account`` / ``None`` gate.
* :func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`
  — the live operator-context KV-v2 read. Single source of truth across
  vROps, vRLI, and Fleet (G3.10-T2 #946). Re-exported here so Fleet's
  public API reads cohesively without forcing consumers to reach into
  the shared module.

The :class:`VcfFleetTargetLike` Protocol captures the minimum target
shape the connector reads: ``name`` (per-target credential cache key),
``host`` / ``port`` (forwarded to :meth:`HttpConnector._base_url`),
``secret_ref`` (the Vault path the loader resolves), and ``auth_model``
(checked at the boundary). No ``sso_realm`` field — Fleet's Basic auth
header carries ``username:password`` directly with no realm suffix
(verified against ``scripts/vcf-fleet.sh`` — the wrapper passes
``-u "${FLEET_USERNAME}:${FLEET_PASSWORD}"`` with no additional
realm/domain decoration).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    load_credentials_from_vault,
)

__all__ = [
    "SessionCredentials",
    "VcfFleetCredentialsLoader",
    "VcfFleetTargetLike",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :data:`VcfFleetCredentialsLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values map to the Basic-auth components the
    connector sends on every Fleet API request — no session token is
    established (HTTP Basic per request, same shape as the Harbor
    precedent).
    """

    username: str
    password: str


@runtime_checkable
class VcfFleetTargetLike(Protocol):
    """Minimum target shape :class:`VcfFleetConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224 — closed) satisfies this
    Protocol unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``str | None`` matches the
    concrete ``Target.secret_ref`` column (nullable) and the shared
    :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
    the loader forwards to; an unset ``secret_ref`` is rejected with a
    clear error inside the loader, never a bare ``KeyError``. ``port``
    is optional — Fleet defaults to 443 and
    :meth:`HttpConnector._base_url` already handles the
    ``port is None or 443`` case correctly.

    No ``sso_realm`` field — Fleet's local user store accepts
    ``admin@local`` as the literal Basic-auth username; no realm suffix
    is appended by the connector.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)`` cache
    key the shared :class:`CredentialsCache` uses, so two same-named targets
    in different tenants never share a cached credential (#1642).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


VcfFleetCredentialsLoader = VcfCredentialsLoader
"""Async callable resolving a ``(target, operator)`` pair to credentials.

Type alias for :data:`~meho_backplane.connectors._shared.vcf_auth.VcfCredentialsLoader`.
Re-exported under a connector-flavoured name so the public API of the
:mod:`meho_backplane.connectors.vcf_fleet` package reads cohesively
(``VcfFleetConnector(credentials_loader=...)``) without exposing the
shared module name at the boundary.

The connector's
:class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
invokes the loader on first ``auth_headers`` call per target and caches
the resulting dict under ``target.name``. The return type is the looser
``dict[str, str]`` (not :class:`SessionCredentials`) because Python
:class:`Protocol` instances aren't runtime-constructible without a
matching class — production code returns a plain dict and the connector
reads ``creds["username"]`` / ``creds["password"]`` by key.
"""

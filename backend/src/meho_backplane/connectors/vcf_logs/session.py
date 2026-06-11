# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target shape + credentials loader wiring for the vcf_logs connector.

The :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
trades operator-context Vault reads to a session token via vRLI's
``POST /api/v2/sessions`` endpoint (JSON body
``{username, password, provider}`` → ``{sessionId, ttl}`` in response, then
``Authorization: Bearer <sessionId>`` on subsequent calls). The credential
fetch (Vault path → service-account ``{"username": ..., "password": ...}``
dict) is split out behind the cross-connector
:data:`~meho_backplane.connectors._shared.vcf_auth.VcfCredentialsLoader`
callable so:

* Production deploys override the default loader at construction time once
  the operator-context per-target Vault credential read lands for the VCF
  management-plane connectors.
* Unit tests inject their own (stub) loader returning a pre-built dict.
* Integration tests against a recorded-fixture or live vRLI target pass a
  loader that yields the appropriate service-account credentials.

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until the Vault read path lands — same posture
the NSX precedent established, and the shared module's default loader
already implements verbatim.

The :class:`VcfLogsTargetLike` Protocol extends the cross-connector
:class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike` shape
with an optional ``provider`` field naming the vRLI identity-source
(``"Local"`` / ``"ActiveDirectory"`` / ``"vIDM"``). The wrapper defaults
to ``"Local"`` when the field is unset; the connector mirrors that
default. Once the concrete ``Target`` model in
:mod:`meho_backplane.targets` adds the column, the model satisfies the
Protocol structurally.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    load_credentials_from_vault,
)

__all__ = [
    "VcfCredentialsLoader",
    "VcfLogsTargetLike",
    "load_credentials_from_vault",
]


@runtime_checkable
class VcfLogsTargetLike(Protocol):
    """Minimum target shape :class:`VcfLogsConnector` reads.

    Extends the cross-connector
    :class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike`
    Protocol with an optional ``provider`` field that names the vRLI
    identity-source for the session-login POST. Structural Protocol -- the
    concrete ``Target`` model in :mod:`meho_backplane.targets` satisfies
    this Protocol unchanged once the ``provider`` column lands; until
    then, callers can pass a stub dataclass.

    Fields:

    * ``id`` / ``tenant_id`` -- the tenant-unique ``(tenant_id, id)``
      cache key
      (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
      the credential + session-token caches use, so two same-named targets
      in different tenants never share a cached session (#1642).
    * ``name`` -- used in audit-log / error messages (no longer a cache key).
    * ``host`` / ``port`` -- forwarded to
      :meth:`HttpConnector._base_url`.
    * ``secret_ref`` -- Vault path the loader resolves to a
      ``{"username": str, "password": str}`` dict.
    * ``auth_model`` -- checked at the boundary by
      :func:`is_acceptable_auth_model`; only
      ``"shared_service_account"`` / ``None`` accepted in v0.2.
    * ``provider`` -- optional vRLI identity-source name
      (``"Local"`` / ``"ActiveDirectory"`` / ``"vIDM"``). When unset
      or empty the connector defaults to ``"Local"``, matching the
      wrapper's posture.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    provider: str | None

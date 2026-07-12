# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential + target shape for the vROps (VCF Operations 9.0) connector.

The hand-rolled
:class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`
reads service-account credentials from the target's Vault path, acquires a
session token via ``POST /suite-api/api/auth/token/acquire``, and presents it
as ``Authorization: OpsToken <token>`` on every request — VCF Operations 9.0.2
rejects stateless HTTP Basic (#2395).

The credential fetch (Vault path → ``{"username": ..., "password": ...}`` dict)
is delegated to the shared :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
and :data:`~meho_backplane.connectors._shared.vcf_auth.VcfCredentialsLoader`
contract — the VCF management-plane connectors (vROps #829, vRLI #830,
Fleet #831) share the same load-once-per-target cache shape and the same
``RuntimeError``-naming-target contract for missing keys (#841).

vROps adds one product-specific field on top of the shared target Protocol:
``auth_source``. When set, it rides the **acquire body** as ``"authSource"``
so vROps routes the login to a non-local identity domain (``Local``, ``vIDM``,
an Active Directory realm name, etc.). When unset (``None`` or empty), the
field is omitted and vROps authenticates against its default local realm. The
exact acceptable values are operator-configured per vROps deployment; the
connector passes the string through verbatim.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    load_credentials_from_vault,
)

__all__ = [
    "VcfOperationsCredentialsLoader",
    "VcfOperationsTargetLike",
    "load_credentials_from_vault",
]


VcfOperationsCredentialsLoader = VcfCredentialsLoader
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

Type alias for :data:`~meho_backplane.connectors._shared.vcf_auth.VcfCredentialsLoader`.
Re-exported under a connector-flavoured name so the public API of the
:mod:`meho_backplane.connectors.vcf_operations` package reads cohesively
(``VcfOperationsConnector(credentials_loader=...)``) without exposing the
shared module name at the boundary.
"""


@runtime_checkable
class VcfOperationsTargetLike(Protocol):
    """Minimum target shape :class:`VcfOperationsConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224) satisfies this unchanged. Extends
    the shared :class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike`
    base with one product-specific field:

    * ``auth_source`` — optional vROps auth-source name. When set, it rides
      the ``token/acquire`` body as ``"authSource"``. When ``None`` (or
      empty), the field is omitted and vROps uses its default local realm.
      The accepted values (``Local``, ``vIDM``, an AD realm name, etc.) are
      operator-configured per vROps deployment; the connector passes the
      string through verbatim.

    The base shared fields (``id``, ``tenant_id``, ``name``, ``host``,
    ``port``, ``secret_ref``, ``auth_model``) are gated and consumed
    identically to the other G3.6 skeletons (see :class:`VcfTargetLike`).
    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)`` cache
    key the shared :class:`CredentialsCache` uses (#1642).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    auth_source: str | None

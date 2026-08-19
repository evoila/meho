# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading + target shape for the ``vcd`` (VMware Cloud Director) connector.

The hand-rolled
:class:`~meho_backplane.connectors.vcd.connector.VcloudDirectorConnector`
authenticates against vCD's provider (System-org) session endpoint with HTTP
Basic and caches the minted Bearer JWT (see :mod:`._auth`). The credential
contract is the shared VCF-family one — a ``{"username": str, "password": str}``
pair — so this module reuses the shared
:mod:`meho_backplane.connectors._shared.vcf_auth` scaffolding (the #841
cross-cutting module) rather than growing a bespoke duplicate:

* :data:`VcloudDirectorTargetLike` — the minimum target shape the connector
  reads. Aliased to the shared
  :class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike` Protocol
  (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model`` + the
  ``id`` / ``tenant_id`` cache-key pair); the concrete ``Target`` model in
  :mod:`meho_backplane.targets` satisfies it structurally.
* :data:`VcloudDirectorCredentialsLoader` — the async ``(target, operator) ->
  dict[str, str]`` loader type. Injectable on connector construction so unit
  tests pass a stub, integration tests a lab-account loader, and production the
  default Vault read.
* :func:`load_credentials_from_vault` — the shared operator-context KV-v2 read,
  re-exported so the package's public API reads cohesively.

The provider **Basic** username is composed as ``"<user>@System"`` — vCD
provider accounts always live in the System org — inside :mod:`._auth`; the
loader returns the raw ``username`` / ``password`` pair. A non-System /
SSO provider identity is a documented follow-up (the connector docstring).
"""

from __future__ import annotations

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    VcfTargetLike,
    load_credentials_from_vault,
)

__all__ = [
    "VcloudDirectorCredentialsLoader",
    "VcloudDirectorTargetLike",
    "load_credentials_from_vault",
]

#: Minimum target shape :class:`VcloudDirectorConnector` reads. Aliased to the
#: shared VCF-family Protocol — vCD reads exactly the fields the sibling VCF
#: connectors do (``name`` / ``host`` / ``port`` / ``secret_ref`` /
#: ``auth_model`` + the ``(tenant_id, id)`` cache key), so a bespoke duplicate
#: Protocol would only drift.
VcloudDirectorTargetLike = VcfTargetLike

#: Async ``(target, operator) -> dict[str, str]`` credential loader. Aliased to
#: the shared VCF loader type; re-exported under a vCD-flavoured name so
#: ``VcloudDirectorConnector(credentials_loader=...)`` reads cohesively without
#: exposing the shared module at the boundary.
VcloudDirectorCredentialsLoader = VcfCredentialsLoader

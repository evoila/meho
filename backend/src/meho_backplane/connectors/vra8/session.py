# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading + target shape for the ``vra8`` (vRealize Automation 8.x) connector.

The hand-rolled :class:`~meho_backplane.connectors.vra8.connector.Vra8Connector`
authenticates against vRA 8's two-step token exchange (CSP identity →
refresh_token → IaaS bearer, see :mod:`._auth`) and caches the minted bearer. The
credential contract is the shared VCF-family one — a ``{"username": str,
"password": str}`` pair — so this module reuses the shared
:mod:`meho_backplane.connectors._shared.vcf_auth` scaffolding (the #841
cross-cutting module) rather than growing a bespoke duplicate, exactly as the
sibling ``vcf-automation`` and ``vcd`` connectors do:

* :data:`Vra8TargetLike` — the minimum target shape the connector reads. Aliased
  to the shared :class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike`
  Protocol (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model`` + the
  ``id`` / ``tenant_id`` cache-key pair); the concrete ``Target`` model satisfies
  it structurally. vRA 8's optional CSP ``domain`` is read via ``getattr`` in
  :mod:`._auth` (local/basic users omit it), so it need not widen the Protocol.
* :data:`Vra8CredentialsLoader` — the async ``(target, operator) -> dict[str, str]``
  loader type. Injectable on connector construction so unit tests pass a stub and
  production the default Vault read.
* :func:`load_credentials_from_vault` — the shared operator-context KV-v2 read,
  re-exported so the package's public API reads cohesively.
"""

from __future__ import annotations

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    VcfTargetLike,
    load_credentials_from_vault,
)

__all__ = [
    "Vra8CredentialsLoader",
    "Vra8TargetLike",
    "load_credentials_from_vault",
]

#: Minimum target shape :class:`Vra8Connector` reads. Aliased to the shared
#: VCF-family Protocol — vRA 8 reads exactly the fields the sibling VCF connectors
#: do (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model`` + the
#: ``(tenant_id, id)`` cache key), plus an optional ``domain`` read via ``getattr``.
Vra8TargetLike = VcfTargetLike

#: Async ``(target, operator) -> dict[str, str]`` credential loader. Aliased to the
#: shared VCF loader type; re-exported under a vRA-8-flavoured name so
#: ``Vra8Connector(credentials_loader=...)`` reads cohesively without exposing the
#: shared module at the boundary.
Vra8CredentialsLoader = VcfCredentialsLoader

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-connector shared helpers.

Exports:

* :mod:`meho_backplane.connectors._shared.vcf_auth` — the common subset
  of auth scaffolding the four VCF management-plane connectors
  (vROps #829, vRLI #830, Fleet #831; Automation #832 is intentionally
  excluded — its dual-plane shape doesn't fit) all share.
* :mod:`meho_backplane.connectors._shared.system_operator` — the
  synthesised system :class:`~meho_backplane.auth.operator.Operator` the
  operator-less connector probe/fingerprint paths thread to the HTTP auth
  surface (G3.9-T1).
* :mod:`meho_backplane.connectors._shared.vault_creds` — the single
  reusable operator-context Vault KV-v2 basic-credentials reader
  (G3.9-T2 #941). Every REST connector loader resolves a target's
  ``secret_ref`` to vendor credentials through it.
* :mod:`meho_backplane.connectors._shared.gsm_creds` — the GCP Secret
  Manager credential backend (#2230), registered under kind ``gsm`` on the
  #2229 resolver seam. Imported here so its ``register_credential_backend``
  call runs eagerly (as ``vault_creds`` does for ``vault``) and the ``gsm``
  kind is present before any credential resolution.
* :mod:`meho_backplane.connectors._shared.cache_key` — the canonical
  tenant-unique ``(tenant_id, id)`` per-target cache key (#1642). Every
  connector credential / session / client cache derives its key here so
  same-named targets in different tenants never collapse to one entry.

New cross-connector shared modules land alongside these — keep each
module focused on a single concern (auth, retries, pagination, etc.)
rather than growing this package into a god-module.
"""

from meho_backplane.connectors._shared import (
    cache_key,
    gsm_creds,
    system_operator,
    vault_creds,
    vcf_auth,
)

__all__ = ["cache_key", "gsm_creds", "system_operator", "vault_creds", "vcf_auth"]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-connector shared helpers.

Exports:

* :mod:`meho_backplane.connectors._shared.vcf_auth` — the common subset
  of auth scaffolding the four VCF management-plane connectors
  (vROps #829, vRLI #830, Fleet #831; Automation #832 is intentionally
  excluded — its dual-plane shape doesn't fit) all share.
* :mod:`meho_backplane.connectors._shared.vault_creds` — the single
  reusable operator-context Vault KV-v2 basic-credentials reader
  (G3.9-T2 #941). Every REST connector loader resolves a target's
  ``secret_ref`` to vendor credentials through it.

New cross-connector shared modules land alongside these — keep each
module focused on a single concern (auth, retries, pagination, etc.)
rather than growing this package into a god-module.
"""

from meho_backplane.connectors._shared import vault_creds, vcf_auth

__all__ = ["vault_creds", "vcf_auth"]

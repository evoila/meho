# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-connector shared helpers.

Currently exports :mod:`meho_backplane.connectors._shared.vcf_auth` — the
common subset of auth scaffolding the four VCF management-plane connectors
(vROps #829, vRLI #830, Fleet #831; Automation #832 is intentionally
excluded — its dual-plane shape doesn't fit) all share.

See :mod:`.vcf_auth` for the public surface. New cross-connector shared
modules land alongside ``vcf_auth.py`` — keep each module focused on a
single concern (auth, retries, pagination, etc.) rather than growing this
package into a god-module.
"""

from meho_backplane.connectors._shared import vcf_auth

__all__ = ["vcf_auth"]

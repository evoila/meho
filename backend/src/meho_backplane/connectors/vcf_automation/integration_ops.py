# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The integration ops on the VCF Automation tenant plane ("VM Apps" organizations).

The five ops (integrations, IPAM provider packages, Git content sources) are
written once for this connector and for Aria Automation 8
(:mod:`meho_backplane.connectors.vmware_shared.vcfa_integration_mixin`). Here
they get the ``vcfa.tenant.`` id prefix and the group
``vcfa-tenant-integrations``. Every path they send is a tenant path
(:func:`._routing.plane_for_path`), so they log in with the tenant login.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vmware_shared.vcfa_integration_mixin import (
    register_integration_ops,
)
from meho_backplane.connectors.vmware_shared.vcfa_integration_ops import INTEGRATION_OPS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["VCFA_INTEGRATION_GROUP", "register_vcfa_integration_operations"]

VCFA_INTEGRATION_GROUP: Final[str] = "vcfa-tenant-integrations"

_wrong_plane = [spec.name for spec in INTEGRATION_OPS if plane_for_path(spec.path) != "tenant"]
if _wrong_plane:
    raise RuntimeError(f"integration ops with a non-tenant path: {_wrong_plane}")


async def register_vcfa_integration_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Register the five integration ops on ``vcfa-rest-9.0``."""
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector

    await register_integration_ops(
        VcfAutomationConnector,
        group_key=VCFA_INTEGRATION_GROUP,
        plane_tag="tenant",
        embedding_service=embedding_service,
    )

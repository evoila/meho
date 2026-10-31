# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The integration ops on Aria Automation 8 (vRealize Automation 8).

The five ops (integrations, IPAM provider packages, Git content sources) are
written once for this connector and for VCF Automation 9
(:mod:`meho_backplane.connectors.vmware_shared.vcfa_integration_mixin`). Here
they get the ``vcfa.vra8.`` id prefix and the group ``vcfa-vra8-integrations``.
The one vRA 8 bearer works on every path they send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from meho_backplane.connectors.vmware_shared.vcfa_integration_mixin import (
    register_integration_ops,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["VRA8_INTEGRATION_GROUP", "register_vra8_integration_operations"]

VRA8_INTEGRATION_GROUP: Final[str] = "vcfa-vra8-integrations"


async def register_vra8_integration_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Register the five integration ops on ``vcfa-vra8-8.0``."""
    from meho_backplane.connectors.vra8.connector import Vra8Connector

    await register_integration_ops(
        Vra8Connector,
        group_key=VRA8_INTEGRATION_GROUP,
        plane_tag="vra8",
        embedding_service=embedding_service,
    )

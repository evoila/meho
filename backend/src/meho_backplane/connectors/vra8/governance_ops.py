# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed vRA 8 ops for approvals, policies, ABX actions and event subscriptions.

The nine ops are shared with the VCF Automation connector. The metadata comes
from :mod:`meho_backplane.connectors.vmware_shared.vra_governance_ops` and
:mod:`~meho_backplane.connectors.vmware_shared.vra_extensibility_ops` with the
``vcfa.vra8`` prefix; the handlers are the methods of
:class:`~meho_backplane.connectors.vmware_shared.vra_handlers.VraSharedHandlers`,
which :class:`~.connector.Vra8Connector` inherits. The one vRA 8 bearer works
on every service, these included.

The ops have their own registrar, queued in this package's ``__init__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from meho_backplane.connectors.vmware_shared.vra_extensibility import (
    register_extensibility_previews,
)
from meho_backplane.connectors.vmware_shared.vra_extensibility_ops import (
    extensibility_op_specs,
    extensibility_when_to_use,
)
from meho_backplane.connectors.vmware_shared.vra_governance_ops import (
    VraOpSpec,
    governance_op_specs,
    governance_when_to_use,
    register_vra_shared_ops,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "GOVERNANCE_GROUP_KEYS",
    "GOVERNANCE_PREFIX",
    "VRA8_GOVERNANCE_OPS",
    "VRA8_GOVERNANCE_WHEN_TO_USE_BY_GROUP",
    "register_vra8_governance_operations",
]

_log = structlog.get_logger(__name__)

GOVERNANCE_PREFIX: Final[str] = "vcfa.vra8"
_PRODUCT: Final[str] = "Aria Automation 8 (vRealize Automation 8)"

#: Shared group name -> this connector's group key.
GOVERNANCE_GROUP_KEYS: Final[dict[str, str]] = {
    "governance": "vcfa-vra8-governance",
    "extensibility": "vcfa-vra8-extensibility",
}

VRA8_GOVERNANCE_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    "vcfa-vra8-governance": governance_when_to_use(GOVERNANCE_PREFIX, _PRODUCT),
    "vcfa-vra8-extensibility": extensibility_when_to_use(GOVERNANCE_PREFIX, _PRODUCT),
}

#: The nine ops: approvals and policies, then ABX actions and subscriptions.
VRA8_GOVERNANCE_OPS: Final[tuple[VraOpSpec, ...]] = (
    *governance_op_specs(GOVERNANCE_PREFIX),
    *extensibility_op_specs(GOVERNANCE_PREFIX),
)

register_extensibility_previews(GOVERNANCE_PREFIX)


async def register_vra8_governance_operations(
    *, embedding_service: EmbeddingService | None = None
) -> None:
    """Upsert the nine ops as typed descriptor rows of the vRA 8 connector."""
    from meho_backplane.connectors.vra8.connector import Vra8Connector

    count = await register_vra_shared_ops(
        Vra8Connector,
        VRA8_GOVERNANCE_OPS,
        group_keys=GOVERNANCE_GROUP_KEYS,
        group_texts=VRA8_GOVERNANCE_WHEN_TO_USE_BY_GROUP,
        extra_tags=("vra8", "vra"),
        embedding_service=embedding_service,
    )
    _log.info("vra8_governance_operations_registered", count=count)

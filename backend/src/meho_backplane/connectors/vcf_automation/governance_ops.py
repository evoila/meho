# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for approvals, policies, ABX actions and event subscriptions.

The nine ops are shared with the vRA 8 connector. The metadata comes from
:mod:`meho_backplane.connectors.vmware_shared.vra_governance_ops` and
:mod:`~meho_backplane.connectors.vmware_shared.vra_extensibility_ops` with the
``vcfa.tenant`` prefix; the handlers are the methods of
:class:`~meho_backplane.connectors.vmware_shared.vra_handlers.VraSharedHandlers`,
which :class:`~.connector.VcfAutomationConnector` inherits. A VM Apps org of VCF
Automation 9 serves the same ``/approval``, ``/policy``, ``/abx`` and
``/event-broker`` APIs as Aria Automation 8; all of them ride the tenant
login (:func:`._routing.plane_for_path`).

The ops have their own registrar, queued in this package's ``__init__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from meho_backplane.connectors.vcf_automation._routing import plane_for_path
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
    "VCFA_GOVERNANCE_OPS",
    "VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP",
    "register_vcfa_governance_operations",
]

_log = structlog.get_logger(__name__)

GOVERNANCE_PREFIX: Final[str] = "vcfa.tenant"
_PRODUCT: Final[str] = "a VCF Automation 9 VM Apps org, on the tenant plane"

#: Shared group name -> this connector's group key.
GOVERNANCE_GROUP_KEYS: Final[dict[str, str]] = {
    "governance": "vcfa-tenant-governance",
    "extensibility": "vcfa-tenant-extensibility",
}

VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    "vcfa-tenant-governance": governance_when_to_use(GOVERNANCE_PREFIX, _PRODUCT),
    "vcfa-tenant-extensibility": extensibility_when_to_use(GOVERNANCE_PREFIX, _PRODUCT),
}

#: The nine ops: approvals and policies, then ABX actions and subscriptions.
VCFA_GOVERNANCE_OPS: Final[tuple[VraOpSpec, ...]] = (
    *governance_op_specs(GOVERNANCE_PREFIX),
    *extensibility_op_specs(GOVERNANCE_PREFIX),
)

for _spec in VCFA_GOVERNANCE_OPS:
    if plane_for_path(_spec.path) != "tenant":
        raise AssertionError(f"VCFA op {_spec.op_id!r} path {_spec.path!r} is not tenant plane")

register_extensibility_previews(GOVERNANCE_PREFIX)


async def register_vcfa_governance_operations(
    *, embedding_service: EmbeddingService | None = None
) -> None:
    """Upsert the nine ops as typed descriptor rows of the VCFA connector."""
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector

    count = await register_vra_shared_ops(
        VcfAutomationConnector,
        VCFA_GOVERNANCE_OPS,
        group_keys=GOVERNANCE_GROUP_KEYS,
        group_texts=VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP,
        extra_tags=("tenant",),
        embedding_service=embedding_service,
    )
    _log.info("vcfa_governance_operations_registered", count=count)

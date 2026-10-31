# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for catalog items and requests (tenant plane).

The six ops are shared with the vRA 8 connector: the metadata comes from
:func:`~meho_backplane.connectors.vmware_shared.vra_catalog_ops.catalog_op_specs`
with the ``vcfa.tenant`` prefix, the handlers from
:mod:`meho_backplane.connectors.vmware_shared.vra_catalog`. A VM Apps org of
VCF Automation 9 serves the same ``/catalog/api`` and ``/deployment/api``
calls as Aria Automation 8; both ride the tenant login
(:func:`._routing.plane_for_path`).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops
from meho_backplane.connectors.vmware_shared.vra_catalog_ops import catalog_op_specs

__all__ = ["CATALOG_GROUP", "CATALOG_PREFIX", "VCFA_CATALOG_OPS"]

CATALOG_PREFIX: Final[str] = "vcfa.tenant"
CATALOG_GROUP: Final[str] = "vcfa-tenant-catalog"

#: The six catalog and request ops, in the order of a request.
VCFA_CATALOG_OPS: Final[tuple[VcfaTypedOp, ...]] = tuple(
    VcfaTypedOp(
        op_id=spec.op_id,
        handler_attr=spec.handler_attr,
        plane="tenant",
        path=spec.path,
        summary=spec.summary,
        description=spec.description,
        parameter_schema=spec.parameter_schema,
        response_schema=spec.response_schema,
        group_key=CATALOG_GROUP,
        tags=(*spec.tags, "tenant"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=spec.llm_instructions,
    )
    for spec in catalog_op_specs(CATALOG_PREFIX)
)


validate_typed_ops(VCFA_CATALOG_OPS)

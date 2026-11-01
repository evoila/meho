# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed vRA 8 ops for cloud accounts, cloud zones, profiles and projects.

The eleven ops are shared with the VCF Automation connector: the metadata
comes from :func:`~meho_backplane.connectors.vcf_automation.infra_ops.infra_ops`
with the ``vcfa.vra8`` prefix, the handlers from
:mod:`meho_backplane.connectors.vcf_automation._infra` and
:mod:`~meho_backplane.connectors.vcf_automation._infra_changes`. Aria Automation
8.18 serves the same ``/iaas/api`` calls as a VCF Automation 9 VM Apps org; the
one vRA 8 bearer covers them.

Two of them exist only here: ``vcfa.vra8.zone.list`` and
``vcfa.vra8.project.create`` (VCF Automation 9 has ``vcfa.tenant.zone.list`` and
``vcfa.tenant.project.create``).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation.infra_ops import infra_ops
from meho_backplane.connectors.vra8.typed_ops import Vra8TypedOp

__all__ = ["INFRA_GROUP", "INFRA_PREFIX", "VRA8_INFRA_OPS"]

INFRA_PREFIX: Final[str] = "vcfa.vra8"
INFRA_GROUP: Final[str] = "vcfa-vra8-infra"

#: The eleven infrastructure ops, in the order of a set-up.
VRA8_INFRA_OPS: Final[tuple[Vra8TypedOp, ...]] = tuple(
    Vra8TypedOp(
        op_id=op.op_id,
        handler_attr=op.handler_attr,
        summary=op.summary,
        description=op.description,
        parameter_schema=op.parameter_schema,
        response_schema=op.response_schema,
        group_key=INFRA_GROUP,
        tags=(*op.tags, "vra8", "vra"),
        safety_level=op.safety_level,
        requires_approval=op.requires_approval,
        llm_instructions=op.llm_instructions,
    )
    for op in infra_ops(INFRA_PREFIX, with_vra8_only=True)
)

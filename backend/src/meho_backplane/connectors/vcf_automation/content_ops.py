# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud template and catalog ops on the VCF Automation tenant plane.

The eleven ops are written once for this connector and for Aria Automation 8
(:mod:`meho_backplane.connectors.vmware_shared.vcfa_content_ops`). Here they
get the ``vcfa.tenant.`` id prefix, the tenant plane and the groups
``vcfa-tenant-templates`` and ``vcfa-tenant-catalog``. The handlers are the
methods :class:`~.connector.VcfAutomationConnector` inherits from
:class:`~meho_backplane.connectors.vmware_shared.vcfa_content.VcfaContentOpsMixin`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops
from meho_backplane.connectors.vmware_shared.vcfa_content import ContentOpSpec, render_text
from meho_backplane.connectors.vmware_shared.vcfa_content_ops import CONTENT_OPS

__all__ = ["VCFA_CONTENT_OPS"]

_NS: Final[str] = "vcfa.tenant"
_GROUP_KEYS: Final[dict[str, str]] = {
    "templates": "vcfa-tenant-templates",
    "catalog": "vcfa-tenant-catalog",
}


def _typed_op(spec: ContentOpSpec) -> VcfaTypedOp:
    return VcfaTypedOp(
        op_id=f"{_NS}.{spec.name}",
        handler_attr=spec.handler_attr,
        plane="tenant",
        path=spec.path,
        summary=render_text(spec.summary, _NS),
        description=render_text(spec.description, _NS),
        parameter_schema=render_text(spec.parameter_schema, _NS),
        response_schema=render_text(spec.response_schema, _NS),
        group_key=_GROUP_KEYS[spec.group],
        tags=(*spec.tags, "tenant"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=render_text(spec.llm_instructions, _NS),
    )


#: The template ops, then the catalog ops.
VCFA_CONTENT_OPS: Final[tuple[VcfaTypedOp, ...]] = tuple(_typed_op(spec) for spec in CONTENT_OPS)

validate_typed_ops(VCFA_CONTENT_OPS)

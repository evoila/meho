# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud template and catalog ops on Aria Automation 8.

The eleven ops are written once for this connector and for VCF Automation 9
(:mod:`meho_backplane.connectors.vmware_shared.vcfa_content_ops`). Here they
get the ``vcfa.vra8.`` id prefix and the groups ``vcfa-vra8-templates`` and
``vcfa-vra8-catalog``. The handlers are the methods
:class:`~.connector.Vra8Connector` inherits from
:class:`~meho_backplane.connectors.vmware_shared.vcfa_content.VcfaContentOpsMixin`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_shared.vcfa_content import ContentOpSpec, render_text
from meho_backplane.connectors.vmware_shared.vcfa_content_ops import CONTENT_OPS
from meho_backplane.connectors.vra8.typed_ops import Vra8TypedOp

__all__ = ["VRA8_CONTENT_OPS"]

_NS: Final[str] = "vcfa.vra8"
_GROUP_KEYS: Final[dict[str, str]] = {
    "templates": "vcfa-vra8-templates",
    "catalog": "vcfa-vra8-catalog",
}


def _typed_op(spec: ContentOpSpec) -> Vra8TypedOp:
    return Vra8TypedOp(
        op_id=f"{_NS}.{spec.name}",
        handler_attr=spec.handler_attr,
        summary=render_text(spec.summary, _NS),
        description=render_text(spec.description, _NS),
        parameter_schema=render_text(spec.parameter_schema, _NS),
        response_schema=render_text(spec.response_schema, _NS),
        group_key=_GROUP_KEYS[spec.group],
        tags=(*spec.tags, "vra8"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=render_text(spec.llm_instructions, _NS),
    )


#: The template ops, then the catalog ops.
VRA8_CONTENT_OPS: Final[tuple[Vra8TypedOp, ...]] = tuple(_typed_op(spec) for spec in CONTENT_OPS)

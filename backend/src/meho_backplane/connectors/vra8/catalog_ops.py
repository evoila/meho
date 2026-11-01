# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed vRA 8 ops for catalog items and requests.

The six ops are shared with the VCF Automation connector: the metadata comes
from
:func:`~meho_backplane.connectors.vmware_shared.vra_catalog_ops.catalog_op_specs`
with the ``vcfa.vra8`` prefix, the handlers from
:mod:`meho_backplane.connectors.vmware_shared.vra_catalog`.

``vcfa.vra8.catalog.item.list`` reads ``/catalog/api/items``: the items the
caller's projects may request. The older ``vcfa.vra8.catalog-item.list``
stays: it reads the admin view of every project (``/catalog/api/admin/items``).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_shared.vra_catalog import (
    catalog_item_request_park_check,
)
from meho_backplane.connectors.vmware_shared.vra_catalog_ops import catalog_op_specs
from meho_backplane.connectors.vra8.typed_ops import Vra8TypedOp
from meho_backplane.operations._preview import register_park_check

__all__ = ["CATALOG_GROUP", "CATALOG_PREFIX", "VRA8_CATALOG_OPS"]

CATALOG_PREFIX: Final[str] = "vcfa.vra8"
CATALOG_GROUP: Final[str] = "vcfa-vra8-catalog"

#: The six catalog and request ops, in the order of a request.
VRA8_CATALOG_OPS: Final[tuple[Vra8TypedOp, ...]] = tuple(
    Vra8TypedOp(
        op_id=spec.op_id,
        handler_attr=spec.handler_attr,
        summary=spec.summary,
        description=spec.description,
        parameter_schema=spec.parameter_schema,
        response_schema=spec.response_schema,
        group_key=CATALOG_GROUP,
        tags=(*spec.tags, "vra8", "vra"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=spec.llm_instructions,
    )
    for spec in catalog_op_specs(CATALOG_PREFIX)
)

# Before a request waits for approval, its inputs are checked for a plain
# secret, so none is stored in the approval queue.
register_park_check(f"{CATALOG_PREFIX}.catalog.item.request", catalog_item_request_park_check)

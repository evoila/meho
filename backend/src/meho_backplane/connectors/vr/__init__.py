# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vr -- VrConnector package (vSphere Replication).

Importing this package registers :class:`VrConnector` against the v2
connector registry under ``(product="vr", version="9.1",
impl_id="vr-rest")`` plus the product wildcard, and queues the typed-op
registrar so every ``vr.*`` operation (:data:`VR_TYPED_OPS`) lands in
``endpoint_descriptor`` before the first dispatch. The ``>=8.8,<10.0``
range keeps vSphere Replication 8.8 and 9.0 on the same class: all three
versions serve the vSphere Replication REST API v2.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vr.connector import VrConnector
from meho_backplane.connectors.vr.typed_ops import (
    VR_TYPED_OPS,
    VrTypedOp,
    register_vr_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

VR_PRODUCT: Final[str] = "vr"
VR_VERSION: Final[str] = "9.1"
VR_IMPL_ID: Final[str] = "vr-rest"
VR_CONNECTOR_ID: Final[str] = f"{VR_IMPL_ID}-{VR_VERSION}"

register_connector_v2(
    product=VR_PRODUCT,
    version=VR_VERSION,
    impl_id=VR_IMPL_ID,
    cls=VrConnector,
)

# Product wildcard: a target with no version yet still resolves here.
register_connector_v2(
    product=VR_PRODUCT,
    version="",
    impl_id="",
    cls=VrConnector,
)

register_typed_op_registrar(register_vr_typed_operations)

__all__ = [
    "VR_CONNECTOR_ID",
    "VR_IMPL_ID",
    "VR_PRODUCT",
    "VR_TYPED_OPS",
    "VR_VERSION",
    "VrConnector",
    "VrTypedOp",
    "register_vr_typed_operations",
]

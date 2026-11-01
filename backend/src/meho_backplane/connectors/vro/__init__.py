# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vro -- VroConnector package (VMware Orchestrator).

Importing this package registers :class:`VroConnector` against the v2
connector registry under ``(product="vro", version="9.0",
impl_id="vro-rest")`` plus the product wildcard, and queues the typed-op
registrar so the ten Orchestrator reads and the two changes (run a
workflow, cancel a run) land in ``endpoint_descriptor`` before the first
dispatch. Importing it also registers the run preview builder. The
``>=8.0,<10.0`` range keeps Orchestrator 8.x (on its own or inside Aria
Automation 8.x) and the VCF 9 Operations orchestrator on one class: both
serve the same ``/vco/api`` paths.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vro.connector import VroConnector, VroServerVersion
from meho_backplane.connectors.vro.session import (
    VroCredentialsLoader,
    VroTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vro.typed_ops import (
    VRO_TYPED_OPS,
    VRO_TYPED_WHEN_TO_USE_BY_GROUP,
    VroTypedOp,
    register_vro_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

VRO_PRODUCT: Final[str] = "vro"
VRO_VERSION: Final[str] = "9.0"
VRO_IMPL_ID: Final[str] = "vro-rest"
VRO_CONNECTOR_ID: Final[str] = f"{VRO_IMPL_ID}-{VRO_VERSION}"

register_connector_v2(
    product=VRO_PRODUCT,
    version=VRO_VERSION,
    impl_id=VRO_IMPL_ID,
    cls=VroConnector,
)

# Product wildcard: a target with no version yet still resolves here.
register_connector_v2(
    product=VRO_PRODUCT,
    version="",
    impl_id="",
    cls=VroConnector,
)

register_typed_op_registrar(register_vro_typed_operations)

__all__ = [
    "VRO_CONNECTOR_ID",
    "VRO_IMPL_ID",
    "VRO_PRODUCT",
    "VRO_TYPED_OPS",
    "VRO_TYPED_WHEN_TO_USE_BY_GROUP",
    "VRO_VERSION",
    "VroConnector",
    "VroCredentialsLoader",
    "VroServerVersion",
    "VroTargetLike",
    "VroTypedOp",
    "load_credentials_from_vault",
    "register_vro_typed_operations",
]

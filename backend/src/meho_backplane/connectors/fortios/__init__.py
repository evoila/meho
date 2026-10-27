# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.fortios -- the FortiGate connector (FortiOS 7.2 to 7.x).

Importing this package registers :class:`FortiOSConnector` under
``(product="fortigate", version="7.x", impl_id="fortigate-rest")`` plus the
product wildcard, and queues the registrar of the typed operations. FortiOS
has no complete, open API file, so every operation is hand-written (typed).
"""

from typing import Final

from meho_backplane.connectors.fortios.connector import FortiOSConnector
from meho_backplane.connectors.fortios.typed_ops import (
    FORTIOS_GROUP_TEXTS,
    FORTIOS_TYPED_OPS,
    register_fortios_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

FORTIOS_PRODUCT: Final[str] = "fortigate"
FORTIOS_VERSION: Final[str] = "7.x"
FORTIOS_IMPL_ID: Final[str] = "fortigate-rest"
FORTIOS_CONNECTOR_ID: Final[str] = f"{FORTIOS_IMPL_ID}-{FORTIOS_VERSION}"

register_connector_v2(
    product=FORTIOS_PRODUCT,
    version=FORTIOS_VERSION,
    impl_id=FORTIOS_IMPL_ID,
    cls=FortiOSConnector,
)

# Product wildcard: a new target with no detected version still finds the connector.
register_connector_v2(product=FORTIOS_PRODUCT, version="", impl_id="", cls=FortiOSConnector)

register_typed_op_registrar(register_fortios_typed_operations)

__all__ = [
    "FORTIOS_CONNECTOR_ID",
    "FORTIOS_GROUP_TEXTS",
    "FORTIOS_IMPL_ID",
    "FORTIOS_PRODUCT",
    "FORTIOS_TYPED_OPS",
    "FORTIOS_VERSION",
    "FortiOSConnector",
    "register_fortios_typed_operations",
]

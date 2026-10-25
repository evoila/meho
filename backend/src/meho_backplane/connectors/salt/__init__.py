# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.salt -- SaltConnector package.

Importing this package registers :class:`SaltConnector` against the v2
connector registry under ``(product="salt", version="8.18",
impl_id="salt-raas")`` plus the product wildcard, and queues the typed-op
registrar so the six Salt reads land in ``endpoint_descriptor`` before the
first dispatch. The ``>=8.0,<10.0`` range keeps Aria Automation Config 8.x
and VCF 9.1 Salt on the same class: both serve the RaaS RPC API.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.salt.connector import (
    SaltConnector,
    SaltRpcError,
    SaltServerVersion,
)
from meho_backplane.connectors.salt.session import (
    SaltSessionLoader,
    SaltTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.salt.typed_ops import (
    SALT_TYPED_OPS,
    SaltTypedOp,
    register_salt_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

SALT_PRODUCT: Final[str] = "salt"
SALT_VERSION: Final[str] = "8.18"
SALT_IMPL_ID: Final[str] = "salt-raas"
SALT_CONNECTOR_ID: Final[str] = f"{SALT_IMPL_ID}-{SALT_VERSION}"

register_connector_v2(
    product=SALT_PRODUCT,
    version=SALT_VERSION,
    impl_id=SALT_IMPL_ID,
    cls=SaltConnector,
)

# Product wildcard: a target with no version yet still resolves here.
register_connector_v2(
    product=SALT_PRODUCT,
    version="",
    impl_id="",
    cls=SaltConnector,
)

register_typed_op_registrar(register_salt_typed_operations)

__all__ = [
    "SALT_CONNECTOR_ID",
    "SALT_IMPL_ID",
    "SALT_PRODUCT",
    "SALT_TYPED_OPS",
    "SALT_VERSION",
    "SaltConnector",
    "SaltRpcError",
    "SaltServerVersion",
    "SaltSessionLoader",
    "SaltTargetLike",
    "SaltTypedOp",
    "load_session_credentials_from_vault",
    "register_salt_typed_operations",
]

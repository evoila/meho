# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.hcx -- HcxConnector package.

Importing this package registers :class:`HcxConnector` against the v2
connector registry under ``(product="hcx", version="9.0",
impl_id="hcx-rest")`` plus the product wildcard, and queues the typed-op
registrar so ``hcx.api_file.get`` lands in ``endpoint_descriptor`` before
the first dispatch. The class pin tracks the VCF 9 line; the
``>=4.7,<10.0`` range keeps HCX 4.x dispatchable through the same class.
"""

from typing import Final

from meho_backplane.connectors.hcx.connector import HcxConnector, HcxManagerVersion
from meho_backplane.connectors.hcx.session import (
    HcxSessionLoader,
    HcxTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.hcx.typed_ops import (
    HCX_TYPED_OPS,
    HcxTypedOp,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

HCX_PRODUCT: Final[str] = "hcx"
HCX_VERSION: Final[str] = "9.0"
HCX_IMPL_ID: Final[str] = "hcx-rest"
HCX_CONNECTOR_ID: Final[str] = f"{HCX_IMPL_ID}-{HCX_VERSION}"

register_connector_v2(
    product=HCX_PRODUCT,
    version=HCX_VERSION,
    impl_id=HCX_IMPL_ID,
    cls=HcxConnector,
)

# Product wildcard: a target with no version yet still resolves here.
register_connector_v2(
    product=HCX_PRODUCT,
    version="",
    impl_id="",
    cls=HcxConnector,
)

register_typed_op_registrar(register_hcx_typed_operations)

__all__ = [
    "HCX_CONNECTOR_ID",
    "HCX_IMPL_ID",
    "HCX_PRODUCT",
    "HCX_TYPED_OPS",
    "HCX_VERSION",
    "HcxConnector",
    "HcxManagerVersion",
    "HcxSessionLoader",
    "HcxTargetLike",
    "HcxTypedOp",
    "load_session_credentials_from_vault",
    "register_hcx_typed_operations",
]

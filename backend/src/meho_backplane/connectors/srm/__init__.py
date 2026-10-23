# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.srm -- SrmConnector package.

Importing this package registers :class:`SrmConnector` against the v2
connector registry under ``(product="srm", version="9.1",
impl_id="srm-rest")`` plus the product wildcard, and queues the typed-op
registrar so ``srm.about`` and ``srm.api_file.get`` land in
``endpoint_descriptor`` before the first dispatch. The ``>=8.8,<10.0``
range keeps SRM 8.8 and Live Site Recovery 9.0 on the same class: all
three versions serve the SRM REST API v2.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.srm.connector import (
    SrmConnector,
    SrmServerVersion,
)
from meho_backplane.connectors.srm.session import (
    SrmSessionLoader,
    SrmTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.srm.typed_ops import (
    SRM_TYPED_OPS,
    SrmTypedOp,
    register_srm_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

SRM_PRODUCT: Final[str] = "srm"
SRM_VERSION: Final[str] = "9.1"
SRM_IMPL_ID: Final[str] = "srm-rest"
SRM_CONNECTOR_ID: Final[str] = f"{SRM_IMPL_ID}-{SRM_VERSION}"

register_connector_v2(
    product=SRM_PRODUCT,
    version=SRM_VERSION,
    impl_id=SRM_IMPL_ID,
    cls=SrmConnector,
)

# Product wildcard: a target with no version yet still resolves here.
register_connector_v2(
    product=SRM_PRODUCT,
    version="",
    impl_id="",
    cls=SrmConnector,
)

register_typed_op_registrar(register_srm_typed_operations)

__all__ = [
    "SRM_CONNECTOR_ID",
    "SRM_IMPL_ID",
    "SRM_PRODUCT",
    "SRM_TYPED_OPS",
    "SRM_VERSION",
    "SrmConnector",
    "SrmServerVersion",
    "SrmSessionLoader",
    "SrmTargetLike",
    "SrmTypedOp",
    "load_session_credentials_from_vault",
    "register_srm_typed_operations",
]

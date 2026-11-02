# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.phpipam -- the phpIPAM connector (IP address management).

Importing this package registers :class:`PhpipamConnector` as
``("phpipam", "1.x", "phpipam-rest")`` (connector ID ``phpipam-rest-1.x``)
plus the wildcard row ``("phpipam", "", "")``, so every ``phpipam`` target
finds it, also one without a fingerprint. It queues the registrar of the four
typed operations and wires the approval previews of the two changes.

See ``docs/codebase/connectors-phpipam.md``.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.phpipam import op_classes  # noqa: F401 -- declares the op classes
from meho_backplane.connectors.phpipam import writes as _writes  # noqa: F401 -- wires previews
from meho_backplane.connectors.phpipam.connector import PhpipamConnector
from meho_backplane.connectors.phpipam.session import (
    PhpipamCredentials,
    PhpipamCredentialsLoader,
    PhpipamTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.phpipam.typed_ops import (
    PHPIPAM_TYPED_OPS,
    PHPIPAM_WHEN_TO_USE_BY_GROUP,
    PhpipamTypedOp,
    register_phpipam_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

PHPIPAM_PRODUCT: Final[str] = "phpipam"
PHPIPAM_VERSION: Final[str] = "1.x"
PHPIPAM_IMPL_ID: Final[str] = "phpipam-rest"
PHPIPAM_CONNECTOR_ID: Final[str] = f"{PHPIPAM_IMPL_ID}-{PHPIPAM_VERSION}"

register_connector_v2(
    product=PHPIPAM_PRODUCT,
    version=PHPIPAM_VERSION,
    impl_id=PHPIPAM_IMPL_ID,
    cls=PhpipamConnector,
)

register_connector_v2(
    product=PHPIPAM_PRODUCT,
    version="",
    impl_id="",
    cls=PhpipamConnector,
)

register_typed_op_registrar(register_phpipam_typed_operations)

__all__ = [
    "PHPIPAM_CONNECTOR_ID",
    "PHPIPAM_IMPL_ID",
    "PHPIPAM_PRODUCT",
    "PHPIPAM_TYPED_OPS",
    "PHPIPAM_VERSION",
    "PHPIPAM_WHEN_TO_USE_BY_GROUP",
    "PhpipamConnector",
    "PhpipamCredentials",
    "PhpipamCredentialsLoader",
    "PhpipamTargetLike",
    "PhpipamTypedOp",
    "load_credentials_from_vault",
    "register_phpipam_typed_operations",
]

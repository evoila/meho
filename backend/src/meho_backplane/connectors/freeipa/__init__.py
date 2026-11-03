# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.freeipa: the Red Hat Identity Management (FreeIPA) connector.

Importing this package registers :class:`FreeipaConnector` twice in the v2
connector registry:

* ``(product="freeipa", version="4.x", impl_id="freeipa-jsonrpc")``: the
  connector ID ``freeipa-jsonrpc-4.x``, for FreeIPA 4.9 and later 4.x
  releases (Red Hat Enterprise Linux 8 and later).
* ``(product="freeipa", version="", impl_id="")``: the wildcard row, so every
  ``freeipa`` target finds the connector, also a new one without a
  fingerprint.

The product must equal the part of the implementation ID before the first
hyphen (``freeipa``), or the backplane does not start.

It also queues the typed-operation registrar, which stores the FreeIPA
operations at start-up. The connector has typed operations only.
"""

from typing import Final

from meho_backplane.connectors.freeipa.connector import (
    FreeipaConnector,
    FreeipaRequestRefusedError,
)
from meho_backplane.connectors.freeipa.rpc import FreeipaCommandError
from meho_backplane.connectors.freeipa.session import (
    FreeipaCredentialsLoader,
    FreeipaTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.freeipa.typed_ops import (
    FREEIPA_TYPED_OPS,
    FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP,
    FreeipaTypedOp,
    register_freeipa_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

FREEIPA_PRODUCT: Final[str] = "freeipa"
FREEIPA_VERSION: Final[str] = "4.x"
FREEIPA_IMPL_ID: Final[str] = "freeipa-jsonrpc"
FREEIPA_CONNECTOR_ID: Final[str] = f"{FREEIPA_IMPL_ID}-{FREEIPA_VERSION}"

register_connector_v2(
    product=FREEIPA_PRODUCT,
    version=FREEIPA_VERSION,
    impl_id=FREEIPA_IMPL_ID,
    cls=FreeipaConnector,
)

# The wildcard row: a ``freeipa`` target with no version yet still resolves.
# The versioned row above wins when both match.
register_connector_v2(
    product=FREEIPA_PRODUCT,
    version="",
    impl_id="",
    cls=FreeipaConnector,
)

register_typed_op_registrar(register_freeipa_typed_operations)

__all__ = [
    "FREEIPA_CONNECTOR_ID",
    "FREEIPA_IMPL_ID",
    "FREEIPA_PRODUCT",
    "FREEIPA_TYPED_OPS",
    "FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP",
    "FREEIPA_VERSION",
    "FreeipaCommandError",
    "FreeipaConnector",
    "FreeipaCredentialsLoader",
    "FreeipaRequestRefusedError",
    "FreeipaTargetLike",
    "FreeipaTypedOp",
    "load_credentials_from_vault",
    "register_freeipa_typed_operations",
]

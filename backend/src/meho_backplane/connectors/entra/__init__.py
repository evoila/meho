# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.entra: the Microsoft Entra ID connector package.

Importing this package registers :class:`EntraConnector` twice in the v2
connector registry:

* ``(product="entra", version="1.0", impl_id="entra-graph")``: the connector
  ID ``entra-graph-1.0``. The version is the Microsoft Graph API version the
  connector calls (v1.0), because Entra ID has no product version.
* ``(product="entra", version="", impl_id="")``: the wildcard row, so every
  ``entra`` target finds the connector, also a new one without a fingerprint.

The product must equal the part of the implementation ID before the first
hyphen (``entra``), or the backplane does not start.

Entra ID is its own product, apart from ``azure``: directory objects (users,
groups, apps) live in Microsoft Graph, a different API with its own
permissions. The login code is shared with the Azure connector
(``connectors/_shared/microsoft_login.py``); only the token scope differs.

It also queues the typed-operation registrar, which stores the Entra ID
operations at start-up. The connector has typed operations only; nothing is
ingested from a spec.
"""

from typing import Final

from meho_backplane.connectors.entra.connector import EntraConnector
from meho_backplane.connectors.entra.session import (
    EntraCredentials,
    EntraCredentialsLoader,
    EntraTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.entra.typed_ops import (
    ENTRA_TYPED_OPS,
    ENTRA_TYPED_WHEN_TO_USE_BY_GROUP,
    EntraTypedOp,
    register_entra_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

ENTRA_PRODUCT: Final[str] = "entra"
ENTRA_VERSION: Final[str] = "1.0"
ENTRA_IMPL_ID: Final[str] = "entra-graph"
ENTRA_CONNECTOR_ID: Final[str] = f"{ENTRA_IMPL_ID}-{ENTRA_VERSION}"

register_connector_v2(
    product=ENTRA_PRODUCT,
    version=ENTRA_VERSION,
    impl_id=ENTRA_IMPL_ID,
    cls=EntraConnector,
)

# The wildcard row: an ``entra`` target with no version (all of them, since
# Entra ID has none) still resolves. The versioned row above wins when both match.
register_connector_v2(
    product=ENTRA_PRODUCT,
    version="",
    impl_id="",
    cls=EntraConnector,
)

register_typed_op_registrar(register_entra_typed_operations)

__all__ = [
    "ENTRA_CONNECTOR_ID",
    "ENTRA_IMPL_ID",
    "ENTRA_PRODUCT",
    "ENTRA_TYPED_OPS",
    "ENTRA_TYPED_WHEN_TO_USE_BY_GROUP",
    "ENTRA_VERSION",
    "EntraConnector",
    "EntraCredentials",
    "EntraCredentialsLoader",
    "EntraTargetLike",
    "EntraTypedOp",
    "load_credentials_from_vault",
    "register_entra_typed_operations",
]

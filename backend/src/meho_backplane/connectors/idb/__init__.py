# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.idb -- VCF Identity Broker and Workspace ONE Access.

Importing this package registers two implementations of the product ``idb``.
The resolver picks one per target by the target's version:

* :class:`IdbWsaConnector` as ``("idb", "3.3", "idb-wsa")`` (connector
  ``idb-wsa-3.3``), Workspace ONE Access 3.3, band ``>=3.3,<4.0``.
* :class:`IdbRestConnector` as ``("idb", "9.0", "idb-rest")`` (connector
  ``idb-rest-9.0``), VCF Identity Broker 9.x, band ``>=9.0,<10.0``. It also
  owns the wildcard row ``("idb", "", "")``, so a target with no version yet
  resolves here; its fingerprint tells the two products apart.

Both register the same six typed operations. See
``docs/codebase/connectors-idb.md`` and the decision record
``docs/decisions/2026-10-23-identity-broker-connector.md``.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.idb.connector import (
    IdbConnectorBase,
    IdbRestConnector,
    IdbWsaConnector,
)
from meho_backplane.connectors.idb.session import (
    IdbCredentials,
    IdbCredentialsLoader,
    IdbTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.idb.typed_ops import (
    IDB_TYPED_OPS,
    IDB_WHEN_TO_USE_BY_GROUP,
    IdbTypedOp,
    register_idb_rest_typed_operations,
    register_idb_wsa_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

IDB_PRODUCT: Final[str] = "idb"
IDB_WSA_VERSION: Final[str] = "3.3"
IDB_WSA_IMPL_ID: Final[str] = "idb-wsa"
IDB_WSA_CONNECTOR_ID: Final[str] = f"{IDB_WSA_IMPL_ID}-{IDB_WSA_VERSION}"
IDB_REST_VERSION: Final[str] = "9.0"
IDB_REST_IMPL_ID: Final[str] = "idb-rest"
IDB_REST_CONNECTOR_ID: Final[str] = f"{IDB_REST_IMPL_ID}-{IDB_REST_VERSION}"

register_connector_v2(
    product=IDB_PRODUCT,
    version=IDB_WSA_VERSION,
    impl_id=IDB_WSA_IMPL_ID,
    cls=IdbWsaConnector,
)

register_connector_v2(
    product=IDB_PRODUCT,
    version=IDB_REST_VERSION,
    impl_id=IDB_REST_IMPL_ID,
    cls=IdbRestConnector,
)

register_connector_v2(
    product=IDB_PRODUCT,
    version="",
    impl_id="",
    cls=IdbRestConnector,
)

register_typed_op_registrar(register_idb_wsa_typed_operations)
register_typed_op_registrar(register_idb_rest_typed_operations)

__all__ = [
    "IDB_PRODUCT",
    "IDB_REST_CONNECTOR_ID",
    "IDB_REST_IMPL_ID",
    "IDB_REST_VERSION",
    "IDB_TYPED_OPS",
    "IDB_WHEN_TO_USE_BY_GROUP",
    "IDB_WSA_CONNECTOR_ID",
    "IDB_WSA_IMPL_ID",
    "IDB_WSA_VERSION",
    "IdbConnectorBase",
    "IdbCredentials",
    "IdbCredentialsLoader",
    "IdbRestConnector",
    "IdbTargetLike",
    "IdbTypedOp",
    "IdbWsaConnector",
    "load_credentials_from_vault",
    "register_idb_rest_typed_operations",
    "register_idb_wsa_typed_operations",
]

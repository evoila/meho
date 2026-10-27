# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.azure: the Azure connector package.

Importing this package registers :class:`AzureConnector` twice in the v2
connector registry:

* ``(product="azure", version="2026.10", impl_id="azure-arm")``: the
  connector ID ``azure-arm-2026.10``. The version is a date, like
  ``hetzner-rest-2026.04``, because Azure has no product version.
* ``(product="azure", version="", impl_id="")``: the wildcard row, so every
  ``azure`` target finds the connector, also a new one without a fingerprint.

The product must equal the part of the implementation ID before the first
hyphen (``azure``), or the backplane does not start.

It also queues the typed-operation registrar, which stores the Azure
operations at start-up. The connector has typed operations only; nothing is
ingested from a spec.
"""

from typing import Final

from meho_backplane.connectors.azure.connector import AzureConnector
from meho_backplane.connectors.azure.session import (
    AzureCredentials,
    AzureCredentialsLoader,
    AzureTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.azure.typed_ops import (
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AzureTypedOp,
    register_azure_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

AZURE_PRODUCT: Final[str] = "azure"
AZURE_VERSION: Final[str] = "2026.10"
AZURE_IMPL_ID: Final[str] = "azure-arm"
AZURE_CONNECTOR_ID: Final[str] = f"{AZURE_IMPL_ID}-{AZURE_VERSION}"

register_connector_v2(
    product=AZURE_PRODUCT,
    version=AZURE_VERSION,
    impl_id=AZURE_IMPL_ID,
    cls=AzureConnector,
)

# The wildcard row: an ``azure`` target with no version (all of them, since
# Azure has none) still resolves. The versioned row above wins when both match.
register_connector_v2(
    product=AZURE_PRODUCT,
    version="",
    impl_id="",
    cls=AzureConnector,
)

register_typed_op_registrar(register_azure_typed_operations)

__all__ = [
    "AZURE_CONNECTOR_ID",
    "AZURE_IMPL_ID",
    "AZURE_PRODUCT",
    "AZURE_TYPED_OPS",
    "AZURE_TYPED_WHEN_TO_USE_BY_GROUP",
    "AZURE_VERSION",
    "AzureConnector",
    "AzureCredentials",
    "AzureCredentialsLoader",
    "AzureTargetLike",
    "AzureTypedOp",
    "load_credentials_from_vault",
    "register_azure_typed_operations",
]

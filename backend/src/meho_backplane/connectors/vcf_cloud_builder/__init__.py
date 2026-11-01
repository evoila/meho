# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_cloud_builder — the Cloud Builder implementation of ``installer``.

Importing this package registers :class:`CloudBuilderConnector` under
``(product="installer", version="5.0", impl_id="installer-cb")``: connector
id ``installer-cb-5.0``, for Cloud Builder 5.0 to 5.2 (``>=5.0,<9.0``). The
VCF Installer connector (``installer-rest-9.1``, ``>=9.0,<10.0``) is the
other implementation of the same product.

Only the versioned entry is registered. The ``("installer", "", "")``
wildcard belongs to ``installer-rest``, so a target with no version still
resolves there. A Cloud Builder target needs its version set (for example
``5.2``) to resolve here.

The seven operations (:mod:`.typed_ops`) are hand-written, so they work on a
fresh install with no spec ingest.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2

# Importing ``_preview`` registers what the approver sees for the three changes.
from meho_backplane.connectors.vcf_cloud_builder import (
    _preview,  # noqa: F401
    op_classes,  # noqa: F401 -- declares the op classes
)
from meho_backplane.connectors.vcf_cloud_builder.connector import CloudBuilderConnector
from meho_backplane.connectors.vcf_cloud_builder.typed_ops import (
    CLOUD_BUILDER_TYPED_OPS,
    register_cloud_builder_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

CLOUD_BUILDER_PRODUCT: Final[str] = "installer"
CLOUD_BUILDER_VERSION: Final[str] = "5.0"
CLOUD_BUILDER_IMPL_ID: Final[str] = "installer-cb"
CLOUD_BUILDER_CONNECTOR_ID: Final[str] = f"{CLOUD_BUILDER_IMPL_ID}-{CLOUD_BUILDER_VERSION}"

register_connector_v2(
    product=CLOUD_BUILDER_PRODUCT,
    version=CLOUD_BUILDER_VERSION,
    impl_id=CLOUD_BUILDER_IMPL_ID,
    cls=CloudBuilderConnector,
)

register_typed_op_registrar(register_cloud_builder_typed_operations)

__all__ = [
    "CLOUD_BUILDER_CONNECTOR_ID",
    "CLOUD_BUILDER_IMPL_ID",
    "CLOUD_BUILDER_PRODUCT",
    "CLOUD_BUILDER_TYPED_OPS",
    "CLOUD_BUILDER_VERSION",
    "CloudBuilderConnector",
    "register_cloud_builder_typed_operations",
]

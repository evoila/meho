# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.harbor — HarborConnector package.

Importing this package registers :class:`HarborConnector` against the
v2 connector registry under
``(product="harbor", version="2.x", impl_id="harbor-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so the
registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="2.x", impl_id="harbor-rest")`` key; the v1 entry would land as
``("harbor", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.sddc_manager` and
:mod:`meho_backplane.connectors.nsx` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="harbor", version="2.x", impl_id="harbor-rest")`` triple
because this module has already registered the hand-rolled class. Until then,
this module is the only registration path.

Operations for this connector arrive in #620 via G0.7 spec ingestion of
the Harbor 2.x OpenAPI spec against the ``endpoint_descriptor`` table.
The robot lifecycle ops (create/delete) ship in #621.
"""

from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.harbor.core_ops import (
    HARBOR_CONNECTOR_ID,
    HARBOR_CORE_GROUPS,
    HARBOR_CORE_OPS,
    HARBOR_IMPL_ID,
    HARBOR_PATH_RULES,
    HARBOR_PRODUCT,
    HARBOR_VERSION,
    HarborCoreGroup,
    HarborCoreOp,
    apply_harbor_core_curation,
    classify_harbor_op,
)
from meho_backplane.connectors.harbor.session import (
    HarborCredentialsLoader,
    HarborTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

register_connector_v2(
    product="harbor",
    version="2.x",
    impl_id="harbor-rest",
    cls=HarborConnector,
)

__all__ = [
    "HARBOR_CONNECTOR_ID",
    "HARBOR_CORE_GROUPS",
    "HARBOR_CORE_OPS",
    "HARBOR_IMPL_ID",
    "HARBOR_PATH_RULES",
    "HARBOR_PRODUCT",
    "HARBOR_VERSION",
    "HarborConnector",
    "HarborCoreGroup",
    "HarborCoreOp",
    "HarborCredentialsLoader",
    "HarborTargetLike",
    "SessionCredentials",
    "apply_harbor_core_curation",
    "classify_harbor_op",
    "load_credentials_from_vault",
]

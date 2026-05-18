# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.nsx -- NsxConnector package.

Importing this package registers :class:`NsxConnector` against the v2
connector registry under
``(product="nsx", version="4.2", impl_id="nsx-rest")``. The chassis
lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises
an explicit ``(version="4.2", impl_id="nsx-rest")`` key; the v1 entry
would land as ``("nsx", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.vmware_rest`
established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the
idempotency check there will no-op on the
``(product="nsx", version="4.2", impl_id="nsx-rest")`` triple because
this module has already registered the hand-rolled class. Until then,
this module is the only registration path; no behavioural drift
results from the absence of the auto-shim infrastructure.

Operations for this connector arrive in #614 via G0.7 spec ingestion
of ``nsx-4.2/policy.yaml`` + ``nsx-4.2/manager.yaml`` against the
``endpoint_descriptor`` table. This Task ships only the skeleton.
"""

from meho_backplane.connectors.nsx.connector import NsxConnector
from meho_backplane.connectors.nsx.core_ops import (
    NSX_CONNECTOR_ID,
    NSX_CORE_GROUPS,
    NSX_CORE_OPS,
    NSX_IMPL_ID,
    NSX_PATH_RULES,
    NSX_PRODUCT,
    NSX_VERSION,
    NsxCoreGroup,
    NsxCoreOp,
    apply_nsx_core_curation,
    classify_nsx_op,
)
from meho_backplane.connectors.nsx.session import (
    NsxSessionLoader,
    NsxTargetLike,
    SessionCredentials,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

register_connector_v2(
    product="nsx",
    version="4.2",
    impl_id="nsx-rest",
    cls=NsxConnector,
)

__all__ = [
    "NSX_CONNECTOR_ID",
    "NSX_CORE_GROUPS",
    "NSX_CORE_OPS",
    "NSX_IMPL_ID",
    "NSX_PATH_RULES",
    "NSX_PRODUCT",
    "NSX_VERSION",
    "NsxConnector",
    "NsxCoreGroup",
    "NsxCoreOp",
    "NsxSessionLoader",
    "NsxTargetLike",
    "SessionCredentials",
    "apply_nsx_core_curation",
    "classify_nsx_op",
    "load_session_credentials_from_vault",
]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_logs -- VcfLogsConnector package.

Importing this package registers :class:`VcfLogsConnector` against the
v2 connector registry under
``(product="vrli", version="9.0", impl_id="vrli-rest")``. The
``product`` is the dispatch-canonical token
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives
from the ``vrli-rest`` impl_id, so the registration round-trips and an
operator target carrying the natural ``product="vrli"`` token resolves
this connector rather than a shadowing auto-shim (G0.26-T4 #1798
retired the historical ``product="vcf-logs"`` split). The chassis
lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="vrli-rest")`` key; the v1 entry
would land as ``("vrli", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.nsx` /
:mod:`meho_backplane.connectors.vcf_automation` established.

Operations for this connector arrive in #834 via G0.7 spec ingestion
against ``vcf-logs-9.0/openapi.yaml``. This Task ships only the
skeleton.
"""

from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector
from meho_backplane.connectors.vcf_logs.core_ops import (
    VRLI_CONNECTOR_ID,
    VRLI_CORE_GROUPS,
    VRLI_CORE_OPS,
    VRLI_IMPL_ID,
    VRLI_PATH_RULES,
    VRLI_PRODUCT,
    VRLI_VERSION,
    VrliCoreGroup,
    VrliCoreOp,
    apply_vrli_core_curation,
    classify_vrli_op,
)
from meho_backplane.connectors.vcf_logs.session import (
    VcfCredentialsLoader,
    VcfLogsTargetLike,
    load_credentials_from_vault,
)

register_connector_v2(
    product="vrli",
    version="9.0",
    impl_id="vrli-rest",
    cls=VcfLogsConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="vrli",
    version="",
    impl_id="",
    cls=VcfLogsConnector,
)

__all__ = [
    "VRLI_CONNECTOR_ID",
    "VRLI_CORE_GROUPS",
    "VRLI_CORE_OPS",
    "VRLI_IMPL_ID",
    "VRLI_PATH_RULES",
    "VRLI_PRODUCT",
    "VRLI_VERSION",
    "SessionLoginError",
    "VcfCredentialsLoader",
    "VcfLogsConnector",
    "VcfLogsTargetLike",
    "VrliCoreGroup",
    "VrliCoreOp",
    "apply_vrli_core_curation",
    "classify_vrli_op",
    "load_credentials_from_vault",
]

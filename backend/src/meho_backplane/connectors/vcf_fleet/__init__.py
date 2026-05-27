# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_fleet — VcfFleetConnector package.

Importing this package registers :class:`VcfFleetConnector` against the
v2 connector registry under
``(product="vcf-fleet", version="9.0", impl_id="fleet-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="fleet-rest")`` key; the v1 entry
would land as ``("vcf-fleet", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.harbor`
and :mod:`meho_backplane.connectors.vcf_automation` established.

Operations for this connector arrive via G0.7 spec ingestion against
the Fleet (vRSLCM-derived) OpenAPI surface. G3.6-T7 (#831) shipped the
connector skeleton; G3.6-T8 (#835) adds the curated read-only v0.5
core — 8 operator-enabled ops + 6 reviewed groups — via the
:func:`~meho_backplane.connectors.vcf_fleet.core_ops.apply_fleet_core_curation`
substrate call against an already-ingested connector.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector
from meho_backplane.connectors.vcf_fleet.core_ops import (
    FLEET_CONNECTOR_ID,
    FLEET_CORE_GROUPS,
    FLEET_CORE_OPS,
    FLEET_IMPL_ID,
    FLEET_PATH_RULES,
    FLEET_PRODUCT,
    FLEET_VERSION,
    FleetCoreGroup,
    FleetCoreOp,
    apply_fleet_core_curation,
    classify_fleet_op,
)
from meho_backplane.connectors.vcf_fleet.session import (
    SessionCredentials,
    VcfFleetCredentialsLoader,
    VcfFleetTargetLike,
    load_credentials_from_vault,
)

register_connector_v2(
    product="vcf-fleet",
    version="9.0",
    impl_id="fleet-rest",
    cls=VcfFleetConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="vcf-fleet",
    version="",
    impl_id="",
    cls=VcfFleetConnector,
)

__all__ = [
    "FLEET_CONNECTOR_ID",
    "FLEET_CORE_GROUPS",
    "FLEET_CORE_OPS",
    "FLEET_IMPL_ID",
    "FLEET_PATH_RULES",
    "FLEET_PRODUCT",
    "FLEET_VERSION",
    "FleetCoreGroup",
    "FleetCoreOp",
    "SessionCredentials",
    "VcfFleetConnector",
    "VcfFleetCredentialsLoader",
    "VcfFleetTargetLike",
    "apply_fleet_core_curation",
    "classify_fleet_op",
    "load_credentials_from_vault",
]

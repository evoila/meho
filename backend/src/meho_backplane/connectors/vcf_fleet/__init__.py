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

Operations for this connector arrive in #835 via G0.7 spec ingestion
against the Fleet (vRSLCM-derived) OpenAPI surface. This Task ships
only the skeleton.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector
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

__all__ = [
    "SessionCredentials",
    "VcfFleetConnector",
    "VcfFleetCredentialsLoader",
    "VcfFleetTargetLike",
    "load_credentials_from_vault",
]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_automation -- VcfAutomationConnector package.

Importing this package registers :class:`VcfAutomationConnector` against
the v2 connector registry under
``(product="vcf-automation", version="9.0", impl_id="vcfa-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="vcfa-rest")`` key; the v1 entry
would land as ``("vcf-automation", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.nsx` /
:mod:`meho_backplane.connectors.sddc_manager` established.

Operations for this connector arrive via G0.7 dual-plane spec ingestion
-- both the provider plane (``vcf-automation-9.0/cloudapi.yaml``) and
the tenant plane (``vcf-automation-9.0/iaas.yaml``) are ingested under
this single connector with ``spec_source`` tags distinguishing them,
same shape as vSphere's ``vcenter.yaml`` + ``vi-json.yaml``. The
operator-review-time curation helper :func:`apply_vcfa_core_curation`
(G3.6-T11 #836) lands the read-only v0.5 core: 6 provider-plane ops
under 4 groups (``provider-site``, ``provider-orgs``,
``provider-regions``, ``provider-users``) plus 5 tenant-plane ops
under 4 groups (``tenant-about``, ``tenant-projects``,
``tenant-deployments``, ``tenant-blueprints``). The skeleton was
shipped at G3.6-T10 (#832).
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_automation.connector import (
    VcfAutomationConfigurationError,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation.core_ops import (
    VCFA_CONNECTOR_ID,
    VCFA_CORE_GROUPS,
    VCFA_CORE_OPS,
    VCFA_IMPL_ID,
    VCFA_PATH_RULES,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfaCoreGroup,
    VcfaCoreOp,
    apply_vcfa_core_curation,
    classify_vcfa_op,
)
from meho_backplane.connectors.vcf_automation.session import (
    SessionCredentials,
    VcfAutomationCredentialsLoader,
    VcfAutomationTargetLike,
    load_credentials_from_vault,
)

register_connector_v2(
    product="vcf-automation",
    version="9.0",
    impl_id="vcfa-rest",
    cls=VcfAutomationConnector,
)

__all__ = [
    "VCFA_CONNECTOR_ID",
    "VCFA_CORE_GROUPS",
    "VCFA_CORE_OPS",
    "VCFA_IMPL_ID",
    "VCFA_PATH_RULES",
    "VCFA_PRODUCT",
    "VCFA_VERSION",
    "SessionCredentials",
    "VcfAutomationConfigurationError",
    "VcfAutomationConnector",
    "VcfAutomationCredentialsLoader",
    "VcfAutomationTargetLike",
    "VcfaCoreGroup",
    "VcfaCoreOp",
    "apply_vcfa_core_curation",
    "classify_vcfa_op",
    "load_credentials_from_vault",
]

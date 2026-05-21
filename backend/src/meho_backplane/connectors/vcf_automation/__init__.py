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

Operations for this connector arrive in #836 via G0.7 dual-plane spec
ingestion -- both the provider plane (``vcf-automation-9.0/provider.yaml``)
and the tenant plane (``vcf-automation-9.0/tenant.yaml``) are ingested
under this single connector with ``spec_source`` tags distinguishing
them, same shape as vSphere's ``vcenter.yaml`` + ``vi-json.yaml``.
This Task ships only the skeleton.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_automation.connector import (
    VcfAutomationConfigurationError,
    VcfAutomationConnector,
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
    "SessionCredentials",
    "VcfAutomationConfigurationError",
    "VcfAutomationConnector",
    "VcfAutomationCredentialsLoader",
    "VcfAutomationTargetLike",
    "load_credentials_from_vault",
]

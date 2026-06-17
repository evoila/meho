# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_operations — VcfOperationsConnector package.

Importing this package registers :class:`VcfOperationsConnector` against the
v2 connector registry under
``(product="vrops", version="9.0", impl_id="vrops-rest")``.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="9.0", impl_id="vrops-rest")`` key; the v1 entry would land as
``("vrops", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.harbor`,
:mod:`meho_backplane.connectors.sddc_manager`, :mod:`meho_backplane.connectors.nsx`,
and :mod:`meho_backplane.connectors.vcf_automation` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="vrops", version="9.0", impl_id="vrops-rest")`` triple
because this module has already registered the hand-rolled class. Until
then, this module is the only registration path.

Spec-ingested read ops arrive in G3.6-T2 (#833) via G0.7 ingestion of the
vROps ``/suite-api`` OpenAPI spec. This skeleton ships zero operations —
the :meth:`~VcfOperationsConnector.execute` shim exists for ABC compatibility
but ``execute(target, op_id, ...)`` against any ``op_id`` resolves to
"unknown operation" at the dispatcher layer until spec ingestion populates
the ``endpoint_descriptor`` table.

Sibling skeletons landing in the same Initiative wave:

* :mod:`meho_backplane.connectors.vcf_logs` — vRLI (#830).
* :mod:`meho_backplane.connectors.vcf_fleet` — Fleet (#831).
* :mod:`meho_backplane.connectors.vcf_automation` — Automation (#832, already
  merged; intentionally **not** a consumer of ``_shared/vcf_auth.py``).

All four share the same registration shape; vROps + vRLI + Fleet share the
``_shared/vcf_auth.py`` helper module (#841).
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector
from meho_backplane.connectors.vcf_operations.core_ops import (
    VROPS_CONNECTOR_ID,
    VROPS_CORE_GROUPS,
    VROPS_CORE_OPS,
    VROPS_IMPL_ID,
    VROPS_PATH_RULES,
    VROPS_PRODUCT,
    VROPS_VERSION,
    VropsCoreGroup,
    VropsCoreOp,
    apply_vrops_core_curation,
    classify_vrops_op,
)
from meho_backplane.connectors.vcf_operations.session import (
    VcfOperationsCredentialsLoader,
    VcfOperationsTargetLike,
    load_credentials_from_vault,
)

register_connector_v2(
    product="vrops",
    version="9.0",
    impl_id="vrops-rest",
    cls=VcfOperationsConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="vrops",
    version="",
    impl_id="",
    cls=VcfOperationsConnector,
)

__all__ = [
    "VROPS_CONNECTOR_ID",
    "VROPS_CORE_GROUPS",
    "VROPS_CORE_OPS",
    "VROPS_IMPL_ID",
    "VROPS_PATH_RULES",
    "VROPS_PRODUCT",
    "VROPS_VERSION",
    "VcfOperationsConnector",
    "VcfOperationsCredentialsLoader",
    "VcfOperationsTargetLike",
    "VropsCoreGroup",
    "VropsCoreOp",
    "apply_vrops_core_curation",
    "classify_vrops_op",
    "load_credentials_from_vault",
]

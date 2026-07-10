# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.sddc_manager — SddcManagerConnector package.

Importing this package registers :class:`SddcManagerConnector` against the
v2 connector registry under
``(product="sddc", version="9.0", impl_id="sddc-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so the
registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="9.0", impl_id="sddc-rest")`` key; the v1 entry would land as
``("sddc", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.nsx` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="sddc", version="9.0", impl_id="sddc-rest")`` triple
because this module has already registered the hand-rolled class. Until then,
this module is the only registration path.

Operations span two surfaces. The audited 12-read lab-audit set (#2306)
ships as first-class **typed** ops in :mod:`.typed_ops` / :mod:`.typed_reads`
(``source_kind="typed"``), dispatchable on a fresh boot with zero catalog
ingest. The four non-audited curated reads (release, domain detail,
network-pools, bundles) stay as ingested-row curation in :mod:`.core_ops`;
the wider VCF API catalog arrives via G0.7 spec ingestion against the
``endpoint_descriptor`` table and stays browsable as profiled-dispatch
breadth (#2271).
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector
from meho_backplane.connectors.sddc_manager.core_ops import (
    SDDC_CONNECTOR_ID,
    SDDC_CORE_GROUPS,
    SDDC_CORE_OPS,
    SDDC_IMPL_ID,
    SDDC_PATH_RULES,
    SDDC_PRODUCT,
    SDDC_VERSION,
    SddcCoreGroup,
    SddcCoreOp,
    apply_sddc_core_curation,
    classify_sddc_op,
)
from meho_backplane.connectors.sddc_manager.profile import SDDC_EXECUTION_PROFILE
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.sddc_manager.typed_ops import (
    SDDC_TYPED_OPS,
    SddcTypedOp,
    register_sddc_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_connector_v2(
    product="sddc",
    version="9.0",
    impl_id="sddc-rest",
    cls=SddcManagerConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="sddc",
    version="",
    impl_id="",
    cls=SddcManagerConnector,
)

# Queue the audited-read typed-op upsert (#2306) onto the lifespan-driven
# registrar list. The runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the ``source_kind='typed'`` descriptor
# rows land before the first dispatch -- no catalog ingest required.
register_typed_op_registrar(register_sddc_typed_operations)

__all__ = [
    "SDDC_CONNECTOR_ID",
    "SDDC_CORE_GROUPS",
    "SDDC_CORE_OPS",
    "SDDC_EXECUTION_PROFILE",
    "SDDC_IMPL_ID",
    "SDDC_PATH_RULES",
    "SDDC_PRODUCT",
    "SDDC_TYPED_OPS",
    "SDDC_VERSION",
    "SddcCoreGroup",
    "SddcCoreOp",
    "SddcCredentialsLoader",
    "SddcManagerConnector",
    "SddcTargetLike",
    "SddcTypedOp",
    "SessionCredentials",
    "apply_sddc_core_curation",
    "classify_sddc_op",
    "load_credentials_from_vault",
    "register_sddc_typed_operations",
]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.nsx -- NsxConnector package.

Importing this package registers :class:`NsxConnector` against the v2
connector registry under
``(product="nsx", version="9.0", impl_id="nsx-rest")``. The chassis
lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The version pin tracks the VCF-9-aligned NSX product line (#1530):
NSX-T 4.x was renumbered onto the VCF train at VCF 9.0, so a live
appliance reports NSX 9.0.x. The class's
``supported_version_range=">=4.0,<10.0"`` keeps the standalone
NSX-T 4.x line dispatchable through the same class -- the resolver
keys on the :class:`packaging.specifiers.SpecifierSet`, not the
class-pinned ``version``.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises
an explicit ``(version="9.0", impl_id="nsx-rest")`` key; the v1 entry
would land as ``("nsx", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.vmware_rest`
established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the
idempotency check there will no-op on the
``(product="nsx", version="9.0", impl_id="nsx-rest")`` triple because
this module has already registered the hand-rolled class. Until then,
this module is the only registration path; no behavioural drift
results from the absence of the auto-shim infrastructure.

Operations for this connector arrive in #614 via G0.7 spec ingestion
of the NSX ``policy.yaml`` + ``manager.yaml`` corpus against the
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
from meho_backplane.connectors.nsx.typed_ops import (
    NSX_TYPED_OPS,
    NsxTypedOp,
    register_nsx_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_connector_v2(
    product="nsx",
    version="9.0",
    impl_id="nsx-rest",
    cls=NsxConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="nsx",
    version="",
    impl_id="",
    cls=NsxConnector,
)

# Queue the audited-read typed-op upsert (#2302) onto the lifespan-driven
# registrar list. The runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the ``source_kind='typed'`` descriptor
# rows land before the first dispatch -- no catalog ingest required.
register_typed_op_registrar(register_nsx_typed_operations)

__all__ = [
    "NSX_CONNECTOR_ID",
    "NSX_CORE_GROUPS",
    "NSX_CORE_OPS",
    "NSX_IMPL_ID",
    "NSX_PATH_RULES",
    "NSX_PRODUCT",
    "NSX_TYPED_OPS",
    "NSX_VERSION",
    "NsxConnector",
    "NsxCoreGroup",
    "NsxCoreOp",
    "NsxSessionLoader",
    "NsxTargetLike",
    "NsxTypedOp",
    "SessionCredentials",
    "apply_nsx_core_curation",
    "classify_nsx_op",
    "load_session_credentials_from_vault",
    "register_nsx_typed_operations",
]

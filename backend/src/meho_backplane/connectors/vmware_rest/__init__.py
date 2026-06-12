# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vmware_rest — VmwareRestConnector package.

Importing this package registers :class:`VmwareRestConnector` against the
v2 connector registry under
``(product="vmware", version="9.0", impl_id="vmware-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="vmware-rest")`` key; the v1 entry
would land as ``("vmware", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-
break ladder. Same pattern :mod:`meho_backplane.connectors.vault`
established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the
idempotency check there will no-op on the
``(product="vmware", version="9.0", impl_id="vmware-rest")`` triple
because this module has already registered the hand-rolled class. Until
then, this module is the only registration path; no behavioural drift
results from the absence of the auto-shim infrastructure.

Endpoint-descriptor rows for the ~1,275 ingested vCenter REST ops live
under the same ``connector_id="vmware-rest-9.0"``. Registering the class
here is the load-bearing prerequisite for those rows becoming
dispatchable; T2/T3/T4/T5 (#501/#503/#504/#508) carry the rest of the
G3.1 work (vi-json ingestion, composite registration helper, composites
themselves).
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vmware_rest.connector import (
    VmwareRestConnector,
    product_from_line_id,
)
from meho_backplane.connectors.vmware_rest.session import (
    SessionCredentials,
    VsphereSessionLoader,
    VsphereTargetLike,
    load_session_credentials_from_vault,
)

register_connector_v2(
    product="vmware",
    version="9.0",
    impl_id="vmware-rest",
    cls=VmwareRestConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The wildcard's ``supported_version_range`` is
# inherited from :class:`VmwareRestConnector`'s class attribute
# (currently ``">=8.5,<10.0"``); when the target *does* carry a
# version that the range filters out, the wildcard demotes to the
# versioned candidate as designed. The versioned entry above always
# wins when both are present (resolver tie-break step 1).
register_connector_v2(
    product="vmware",
    version="",
    impl_id="",
    cls=VmwareRestConnector,
)

# Side-effect import for the read-composites registrar wiring (G3.1-T5
# #508). The package's __init__ appends
# ``register_vmware_composite_operations`` onto the lifespan-driven
# registrar list via
# :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
# so the 5 ``vmware.composite.*`` rows land in ``endpoint_descriptor``
# during ``run_typed_op_registrars`` -- same lifecycle phase the typed
# Vault ops use.
from meho_backplane.connectors.vmware_rest import composites  # noqa: E402

__all__ = [
    "SessionCredentials",
    "VmwareRestConnector",
    "VsphereSessionLoader",
    "VsphereTargetLike",
    "composites",
    "load_session_credentials_from_vault",
    "product_from_line_id",
]

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

__all__ = [
    "SessionCredentials",
    "VmwareRestConnector",
    "VsphereSessionLoader",
    "VsphereTargetLike",
    "load_session_credentials_from_vault",
    "product_from_line_id",
]

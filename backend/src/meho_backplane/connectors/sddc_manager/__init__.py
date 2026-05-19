# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.sddc_manager — SddcManagerConnector package.

Importing this package registers :class:`SddcManagerConnector` against the
v2 connector registry under
``(product="sddc-manager", version="9.0", impl_id="sddc-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so the
registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="9.0", impl_id="sddc-rest")`` key; the v1 entry would land as
``("sddc-manager", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.nsx` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="sddc-manager", version="9.0", impl_id="sddc-rest")`` triple
because this module has already registered the hand-rolled class. Until then,
this module is the only registration path.

Operations for this connector arrive in #617 via G0.7 spec ingestion of
the SDDC Manager VCF API against the ``endpoint_descriptor`` table. This
Task ships only the skeleton.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)

register_connector_v2(
    product="sddc-manager",
    version="9.0",
    impl_id="sddc-rest",
    cls=SddcManagerConnector,
)

__all__ = [
    "SddcCredentialsLoader",
    "SddcManagerConnector",
    "SddcTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]
